#!/usr/bin/env bb
(ns test.integration
  "Integration tests for constitution.py.
   Starts the server, mocks OpenRouter + GitHub APIs, seeds a JSONL,
   verifies API endpoints, SSE stream, and replay determinism."
  (:require [babashka.process :as p]
            [clojure.string :as str]
            [babashka.fs :as fs]
            [cheshire.core :as json]
            [org.httpkit.server :as http]))

;; ---------------------------------------------------------------------------
;; letlocals
;; ---------------------------------------------------------------------------

(defmacro letlocals [& body]
  (let [all-but-last  (butlast body)
        last-item     (last body)
        last-binding? (and (seq? last-item) (= 'bind (first last-item)))
        last-expr     (if last-binding? (last last-item) last-item)
        bindings      (vec (mapcat (fn [item]
                                     (if (and (seq? item) (= 'bind (first item)))
                                       [(second item) (nth item 2)]
                                       ['_ item]))
                                   all-but-last))]
    `(let ~bindings ~last-expr)))

;; ---------------------------------------------------------------------------
;; helpers
;; ---------------------------------------------------------------------------

(def ^:private ansi-green "\033[32m")
(def ^:private ansi-red   "\033[31m")
(def ^:private ansi-reset "\033[0m")

(def ^:private counts (atom {:pass 0 :fail 0}))

(defn- pass [msg]
  (swap! counts update :pass inc)
  (println (str ansi-green "  ✓ " ansi-reset msg)))

(defn- fail [msg]
  (swap! counts update :fail inc)
  (println (str ansi-red "  ✗ " ansi-reset msg)))

(defn- assert! [pred msg]
  (if pred
    (pass msg)
    (do (fail msg)
        (throw (ex-info (str "FAIL: " msg) {})))))

(defn- pick-port []
  (letlocals
    (bind ss   (java.net.ServerSocket. 0))
    (bind port (.getLocalPort ss))
    (.close ss)
    port))

(defn- wait-for-url
  "Poll URL until response contains expected-substr, up to timeout-ms."
  [url expected-substr timeout-ms]
  (letlocals
    (bind deadline (+ (System/currentTimeMillis) timeout-ms))
    (loop []
      (if (try (str/includes? (slurp url) expected-substr)
               (catch Exception _ false))
        true
        (if (< (System/currentTimeMillis) deadline)
          (do (Thread/sleep 300) (recur))
          false)))))

(defn- project-root
  "Repo root (parent of tests/) when loaded from file; else user.dir."
  []
  (if-let [f *file*]
    (str (fs/parent (fs/parent (fs/absolutize (fs/file f)))))
    (System/getProperty "user.dir")))

(defn- get-json [base-url path]
  (-> (slurp (str base-url path))
      (json/parse-string true)))

(defn- post-json! [base-url path]
  (let [tmp (doto (java.io.File/createTempFile "integration" ".json") .deleteOnExit)
        r   @(p/process ["curl" "-sS" "-o" (.getAbsolutePath tmp)
                         "-w" "%{http_code}" "-X" "POST"
                         (str base-url path) "-H" "Content-Length: 0"]
                        {:out :string :err :string})]
    (when-not (zero? (:exit r))
      (fail (str "curl POST " path " exit " (:exit r) " err: " (:err r)))
      (throw (ex-info "curl failed" {})))
    (let [code (parse-long (str/trim (:out r)))
          body (slurp tmp)]
      (when-not (= 200 code)
        (fail (str "POST " path " HTTP " code " body: " body))
        (throw (ex-info "bad HTTP status" {:code code :body body})))
      (json/parse-string body true))))

;; ---------------------------------------------------------------------------
;; mock OpenRouter server
;; ---------------------------------------------------------------------------

(def ^:private mock-models
  [{"id" "mock/chat-v1" "created" 9999999999}
   {"id" "mock/chat-v2" "created" 9999999998}
   {"id" "mock/chat-v3" "created" 9999999997}])

(defn- make-llm-response
  "Always declare A the winner with ratio 2:1."
  []
  {"choices" [{"message" {"content" (json/generate-string
                                      {"winner" "A"
                                       "ratio"  "2:1"
                                       "explanation" "Side A contributed more."})}}]})

(defn- start-mock-openrouter [port]
  (letlocals
    (bind state (atom {:model-requests 0 :compare-requests 0}))
    (bind handler
      (fn [req]
        (cond
          (and (= :get (:request-method req))
               (str/ends-with? (:uri req) "/models"))
          (do (swap! state update :model-requests inc)
              {:status  200
               :headers {"Content-Type" "application/json"}
               :body    (json/generate-string {"data" mock-models})})

          (and (= :post (:request-method req))
               (str/ends-with? (:uri req) "/chat/completions"))
          (do (swap! state update :compare-requests inc)
              {:status  200
               :headers {"Content-Type" "application/json"}
               :body    (json/generate-string (make-llm-response))})

          :else
          {:status 404 :body "not found"})))
    (bind stop-fn (http/run-server handler {:port port}))
    {:stop-fn stop-fn :state state}))

;; ---------------------------------------------------------------------------
;; mock GitHub API server
;; ---------------------------------------------------------------------------

(def ^:private mock-commits
  [{"sha"    "aabbcc001122"
    "commit" {"author"  {"name" "alice"}
              "message" "feat: add rank centrality"}}
   {"sha"    "ddeeff334455"
    "commit" {"author"  {"name" "bob"}
              "message" "fix: decimal precision in decay rate"}}])

(defn- make-commit-detail [sha author message]
  {"sha"     sha
   "commit"  {"author" {"name" author} "message" message}
   "files"   [{"filename" "constitution.py"
               "patch"    "@@ -1,3 +1,4 @@\n+# new line\n context\n context"}]})

(defn- start-mock-github [port]
  (letlocals
    (bind state (atom {:commit-list-requests 0 :commit-detail-requests 0}))
    (bind sha-map
      (into {} (for [c mock-commits]
                 [(:sha c) (make-commit-detail
                             (:sha c)
                             (get-in c ["commit" "author" "name"])
                             (get-in c ["commit" "message"]))])))
    (bind handler
      (fn [req]
        (let [uri (:uri req)]
          (cond
            ;; GET /repos/:owner/:repo/commits (list)
            (re-find #"/commits$" uri)
            (do (swap! state update :commit-list-requests inc)
                {:status  200
                 :headers {"Content-Type" "application/json"}
                 :body    (json/generate-string mock-commits)})

            ;; GET /repos/:owner/:repo/commits/:sha (detail)
            (re-find #"/commits/[a-f0-9]+" uri)
            (letlocals
              (bind sha  (second (re-find #"/commits/([a-f0-9]+)" uri)))
              (bind body (get sha-map sha (make-commit-detail sha "unknown" "unknown commit")))
              (swap! state update :commit-detail-requests inc)
              {:status  200
               :headers {"Content-Type" "application/json"}
               :body    (json/generate-string body)})

            :else
            {:status 404 :body "not found"}))))
    (bind stop-fn (http/run-server handler {:port port}))
    {:stop-fn stop-fn :state state}))

;; ---------------------------------------------------------------------------
;; pre-seeded JSONL for ledger tests
;; ---------------------------------------------------------------------------

(defn- seed-ledger [path]
  (let [entry {:type         "emission"
               :epoch        0
               :timestamp_ms 1700000000000
               :pool_before  "175824"
               :total_emitted "572.1423838308"
               :pool_after   "175251.857616169"
               :decay_rate   "0.003253356063468"
               :distributions {"alice" "381.4282558872" "bob" "190.7141279436"}
               :ranking       {"alice" "0.6666" "bob" "0.3334"}
               :models_used   ["mock/chat-v1" "mock/chat-v2"]}]
    (spit path (str (json/generate-string entry) "\n"))))

;; ---------------------------------------------------------------------------
;; SSE reader — collect N events from the stream
;; ---------------------------------------------------------------------------

(defn- read-sse-events
  "Collect up to n SSE `data:` payloads via curl -N (babashka blocks HttpURLConnection)."
  [url n timeout-ms]
  (let [sec      (max 1 (long (Math/ceil (/ timeout-ms 1000.0))))
        proc     (p/process ["curl" "-sS" "-N" "-H" "Accept: text/event-stream"
                             "--max-time" (str sec) url]
                            {:out :stream :err :string})
        reader   (java.io.BufferedReader. (java.io.InputStreamReader. (:out proc)))
        events   (atom [])]
    (try
      (loop []
        (when (< (count @events) n)
          (if-let [line (try (.readLine reader) (catch Exception _ nil))]
            (do
              (when (str/starts-with? line "data:")
                (let [raw (str/trim (subs line 5))]
                  (when-not (str/blank? raw)
                    (swap! events conj (json/parse-string raw true)))))
              (recur))
            nil)))
      (finally
        (when-let [p (:proc proc)]
          (.destroyForcibly p))
        (deref proc)))
    @events))

;; ---------------------------------------------------------------------------
;; tests
;; ---------------------------------------------------------------------------

(defn integration [& _args]
  (println "\n━━━ constitution.py integration tests ━━━\n")
  (reset! counts {:pass 0 :fail 0})

  (letlocals
    (bind tmp-dir      (str (fs/create-temp-dir {:prefix "constitution-test-"})))
    (bind jsonl-path   (str tmp-dir "/ledger.jsonl"))
    (bind server-port  (pick-port))
    (bind or-port      (pick-port))  ; openrouter mock
    (bind gh-port      (pick-port))  ; github mock
    (bind base-url     (str "http://127.0.0.1:" server-port))

    ;; genesis ~1 month ago so we're at epoch 1+
    (bind genesis-ms   (str (- (System/currentTimeMillis) (* 35 24 60 60 1000))))

    (bind root          (project-root))
    (bind server-env
      {"SESSION_SECRET"       "test-secret"
       "GENESIS_MS"           genesis-ms
       "JSONL_PATH"           jsonl-path
       "OPENROUTER_API_KEY"   "mock-key"
       "GITHUB_CLIENT_ID"     "mock-gh-id"
       "GITHUB_CLIENT_SECRET" "mock-gh-secret"
       "GITHUB_TOKEN"         "mock-gh-token"
       "REPO"                 "tommy-mor/slug"
       "PORT"                 (str server-port)
       "DISABLE_EPOCH_LOOP"   "1"
       "ALLOW_TEST_TRIGGERS"  "1"
       "OPENROUTER_BASE_URL"  (str "http://127.0.0.1:" or-port)
       "GITHUB_API_BASE_URL"  (str "http://127.0.0.1:" gh-port)
       "PATH"                 (get (into {} (System/getenv)) "PATH" "")})

    (bind !server  (atom nil))
    (bind !server2 (atom nil))
    (bind !or-mock (atom nil))
    (bind !gh-mock (atom nil))

    (try
      (letlocals
        ;; 1. start mock servers
        (println "starting mock OpenRouter and GitHub API servers…")
        (bind or-mock (start-mock-openrouter or-port))
        (reset! !or-mock or-mock)
        (bind gh-mock (start-mock-github gh-port))
        (reset! !gh-mock gh-mock)
        (assert! (some? (:stop-fn or-mock)) "mock OpenRouter started")
        (assert! (some? (:stop-fn gh-mock)) "mock GitHub started")

        ;; 2. seed ledger so pool_remaining has history to read
        (seed-ledger jsonl-path)
        (assert! (fs/exists? jsonl-path) "ledger.jsonl seeded")

        ;; 3. start constitution server
        (println (str "\nstarting server on :" server-port " (data: " tmp-dir ")"))
        (bind server (p/process ["uv" "run" "constitution.py"]
                                {:out :inherit :err :inherit :env server-env
                                 :dir root}))
        (reset! !server server)
        (assert! (wait-for-url (str base-url "/api/epoch") "epoch" 20000)
                 "server responds to /api/epoch")

        ;; 4. /api/epoch shape
        (println "\nchecking /api/epoch…")
        (bind epoch-resp (get-json base-url "/api/epoch"))
        (assert! (int? (:epoch epoch-resp))           "epoch is an integer")
        (assert! (pos? (:epoch epoch-resp))           "epoch > 0 (genesis was 35 days ago)")
        (assert! (string? (:pool_remaining epoch-resp)) "pool_remaining present")
        (assert! (string? (:decay_rate_per_epoch epoch-resp)) "decay_rate present")

        ;; 5. /api/halvening
        (println "\nchecking /api/halvening…")
        (bind halv-resp (get-json base-url "/api/halvening"))
        (assert! (pos? (:jubilee_ms halv-resp))       "jubilee_ms is positive")
        (assert! (> (:jubilee_ms halv-resp)
                    (System/currentTimeMillis))        "jubilee is in the future")
        (assert! (string? (:jubilee_utc halv-resp))   "jubilee_utc is a string")

        ;; 6. /api/ledger — returns seeded entry
        (println "\nchecking /api/ledger…")
        (bind ledger-resp (get-json base-url "/api/ledger"))
        (assert! (= 1 (count ledger-resp))            "1 entry in seeded ledger")
        (assert! (= "emission" (:type (first ledger-resp))) "entry type is emission")

        ;; 7. /api/ranking — most recent emission from seed
        (println "\nchecking /api/ranking…")
        (bind rank-resp (get-json base-url "/api/ranking"))
        (assert! (= 0 (:epoch rank-resp))             "ranking epoch is 0 (seeded)")
        (assert! (some? (get-in rank-resp [:ranking :alice])) "alice in ranking")

        ;; 8. /api/contributor/:user
        (println "\nchecking /api/contributor/alice…")
        (bind contrib-resp (get-json base-url "/api/contributor/alice"))
        (assert! (= "alice" (:contributor contrib-resp)) "contributor is alice")
        (assert! (= 1 (count (:history contrib-resp)))   "alice has 1 emission in history")
        (assert! (pos? (parse-double (:total_earned contrib-resp))) "total_earned > 0")

        ;; 9. SSE connects and sends initial event
        (println "\nchecking /sse initial event…")
        (bind sse-events (read-sse-events (str base-url "/sse") 1 5000))
        (assert! (= 1 (count sse-events))             "received 1 SSE event")
        (assert! (int? (:epoch (first sse-events)))   "initial SSE event has epoch")

        ;; 10. POST /test/emit — full ranking pipeline hits mocks
        (println "\ntriggering /test/emit (epoch 1)…")
        (bind emit-resp (post-json! base-url "/test/emit"))
        (assert! (= "emission" (:type emit-resp))     "emit response type is emission")
        (assert! (= 1 (:epoch emit-resp))             "emit is epoch 1 after seeded epoch 0")
        (assert! (pos? (count (:distributions emit-resp))) "emit has distributions")

        (bind or-state @(:state or-mock))
        (bind gh-state @(:state gh-mock))
        (assert! (pos? (:model-requests or-state))    "OpenRouter /models was called")
        (assert! (>= (:compare-requests or-state) 3) "at least 3 pairwise LLM calls (2 authors × 3 models)")
        (assert! (pos? (:commit-list-requests gh-state)) "GitHub commits list was called")
        (assert! (>= (:commit-detail-requests gh-state) 2) "GitHub per-SHA fetches for each commit")

        (bind ledger2 (get-json base-url "/api/ledger"))
        (assert! (= 2 (count ledger2))              "ledger has 2 entries after emit")
        (bind rank-after (get-json base-url "/api/ranking"))
        (assert! (= 1 (:epoch rank-after))           "latest ranking is epoch 1")

        ;; 11. kill and restart — prove replay determinism
        (println "\nkilling server for replay test…")
        (.destroyForcibly (:proc server))
        (deref server)
        (reset! !server nil)

        (println "restarting server from same JSONL…")
        (bind server2 (p/process ["uv" "run" "constitution.py"]
                                 {:out :inherit :err :inherit :env server-env
                                  :dir root}))
        (reset! !server2 server2)
        (assert! (wait-for-url (str base-url "/api/epoch") "epoch" 20000)
                 "restarted server responds to /api/epoch")

        (bind replayed-ledger (get-json base-url "/api/ledger"))
        (assert! (= 2 (count replayed-ledger))        "ledger still has 2 entries after replay")
        (bind replayed-rank   (get-json base-url "/api/ranking"))
        (assert! (= (get-in rank-after [:ranking :alice])
                    (get-in replayed-rank [:ranking :alice]))
                 "alice's rank score (epoch 1) identical after replay"))

      (finally
        (when-some [s @!server]
          (println "\nkilling server…")
          (.destroyForcibly (:proc s))
          (deref s))
        (when-some [s @!server2]
          (.destroyForcibly (:proc s))
          (deref s))
        (when-some [m @!or-mock] ((:stop-fn m)))
        (when-some [m @!gh-mock] ((:stop-fn m)))
        (fs/delete-tree tmp-dir)))

    (let [{:keys [pass fail]} @counts]
      (if (zero? fail)
        (println (str "\n" ansi-green "━━━ " pass " checks passed ━━━" ansi-reset "\n"))
        (do (println (str "\n" ansi-red "━━━ " fail " FAILED / " pass " passed ━━━" ansi-reset "\n"))
            (System/exit 1))))))

(integration)
