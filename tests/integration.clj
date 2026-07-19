#!/usr/bin/env bb
(ns test.integration
  "Integration tests for constitution.py.
   Starts the server, mocks OpenRouter, creates local Git remotes, seeds a JSONL,
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

(defn- command!
  [argv opts]
  (let [r @(p/process argv (merge {:out :string :err :string} opts))]
    (when-not (zero? (:exit r))
      (throw (ex-info (str "command failed: " (pr-str argv) "\n" (:err r))
                      {:argv argv :result r})))
    (str/trim (:out r))))

(defn- git!
  [dir & args]
  (let [base-env (into {} (System/getenv))
        env      (merge base-env
                        {"GIT_CONFIG_NOSYSTEM" "1"
                         "GIT_AUTHOR_NAME" "Integration"
                         "GIT_AUTHOR_EMAIL" "integration@example.test"
                         "GIT_COMMITTER_NAME" "Integration"
                         "GIT_COMMITTER_EMAIL" "integration@example.test"})]
    (command! (into ["git" "-C" dir] args) {:env env})))

(defn- make-git-repository!
  [tmp-dir id contributor email filename]
  (let [remote (str tmp-dir "/" id ".git")
        work   (str tmp-dir "/" id "-work")]
    (command! ["git" "init" "--bare" remote] {})
    (command! ["git" "init" "-b" "main" work] {})
    (git! work "config" "user.name" contributor)
    (git! work "config" "user.email" email)
    (spit (str work "/" filename) (str contributor " contribution\n"))
    (git! work "add" "--" filename)
    (let [env (merge (into {} (System/getenv))
                     {"GIT_CONFIG_NOSYSTEM" "1"
                      "GIT_AUTHOR_NAME" contributor
                      "GIT_AUTHOR_EMAIL" email
                      "GIT_COMMITTER_NAME" contributor
                      "GIT_COMMITTER_EMAIL" email})]
      (command! ["git" "-C" work "commit" "-m" (str "contribution by " contributor)]
                {:env env}))
    (git! work "remote" "add" "origin" remote)
    (git! work "push" "origin" "main")
    {:id id :url remote :refs ["refs/heads/**"]}))

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
               :discovery_snapshot_id "seeded-discovery"
               :pool_before  "175824"
               :total_emitted "572.1423838308"
               :pool_after   "175251.857616169"
               :decay_rate   "0.003253356063468"
               :distributions {"alice" "381.4282558872" "bob" "190.7141279436"}
               :ranking       {"alice" "0.6666" "bob" "0.3334"}
               :models_used   ["mock/chat-v1" "mock/chat-v2"]
               :evidence_schema_version 2
               :ranking_run_id "seed-rank"
               :ranking_event_id "seed-rank-ev"}]
    (spit path (str (json/generate-string entry) "\n"))))

;; ---------------------------------------------------------------------------
;; SSE reader — collect N events from the stream
;; ---------------------------------------------------------------------------

(def ^:private slug-live-base "https://slug.social")
(def ^:private slug-model-parent "slug/token/commit-ranking/model")

(defn- curl-json!
  "GET url with curl -f; parse JSON or throw."
  [url]
  (let [r @(p/process ["curl" "-sS" "-f" url] {:out :string :err :string})]
    (when-not (zero? (:exit r))
      (fail (str "curl -f failed: " url " exit " (:exit r) " err: " (:err r)))
      (throw (ex-info "curl failed" {:url url})))
    (json/parse-string (:out r) true)))

(defn- first-item-path-from-slug-rank
  "Highest-score ranked item, else first unranked path."
  [rank-data]
  (let [ranked (sort-by (comp - :score)
                        (mapcat :ranking (:components rank-data)))]
    (or (:item (first ranked))
        (first (:unranked_items rank-data)))))

(defn- openrouter-id-from-body
  [body]
  (when (string? body)
    (when-let [m (re-find #"https?://openrouter\.ai/(.+?)\s*$" (str/trim body))]
      (str/trim (second m)))))

(defn- test-live-slug-model-council! [root]
  (if-not (= "1" (System/getenv "RUN_LIVE_SLUG_SOCIAL"))
    (println "\n━━━ live slug.social checks skipped (set RUN_LIVE_SLUG_SOCIAL=1) ━━━\n")
    (letlocals
      (println "\n━━━ live slug.social: /api/v0 + fetch_top_models ━━━\n")
      (bind rank-url (str slug-live-base "/api/v0/rank?parent="
                          (java.net.URLEncoder/encode slug-model-parent "UTF-8")))
      (bind rank (curl-json! rank-url))
      (assert! (not= false (:ok rank)) "slug /api/v0/rank not ok:false")
      (bind item-path (first-item-path-from-slug-rank rank))
      (assert! (some? item-path) "slug rank has a ranked or unranked model item")
      (assert! (str/starts-with? item-path "/slug/token/commit-ranking/model/")
               (str "item under model/ namespace (got " item-path ")"))
      (bind item-param (str/replace-first item-path #"^/" ""))
      (bind item-url (str slug-live-base "/api/v0/item?item="
                          (java.net.URLEncoder/encode item-param "UTF-8")))
      (bind item (curl-json! item-url))
      (assert! (= item-path (:item item)) "slug /api/v0/item path matches rank")
      (assert! (str/includes? (str (:body item)) "openrouter.ai")
               "item body references openrouter.ai")
      (bind expected-id (openrouter-id-from-body (:body item)))
      (assert! (some? expected-id) (str "parse OpenRouter id from body: " (pr-str (:body item))))
      (println "  slug item → OpenRouter id:" expected-id)
      (bind py-env (merge (into {} (System/getenv))
                          {"SESSION_SECRET" "live-slug-test"
                           "GENESIS_MS" "1"
                           "SLUG_SOCIAL_BASE_URL" slug-live-base
                           "SLUG_MODEL_RANK_PARENT" slug-model-parent
                           "OPENROUTER_API_KEY" ""}))
      (bind py @(p/process ["uv" "run" "python" "-c"
                            (str "import asyncio, json, os\n"
                                 "os.environ.setdefault('SESSION_SECRET','x')\n"
                                 "os.environ.setdefault('GENESIS_MS','1')\n"
                                 "os.environ['SLUG_SOCIAL_BASE_URL'] = '"
                                 slug-live-base "'\n"
                                 "os.environ['SLUG_MODEL_RANK_PARENT'] = '"
                                 slug-model-parent "'\n"
                                 "os.environ['OPENROUTER_API_KEY'] = ''\n"
                                 "import constitution\n"
                                 "async def _m():\n"
                                 "    m = await constitution.fetch_top_models(3)\n"
                                 "    print(json.dumps(m))\n"
                                 "asyncio.run(_m())\n")]
                           {:out :string :err :string :dir root :env py-env}))
      (assert! (zero? (:exit py))
               (str "uv run python fetch_top_models exit " (:exit py) " err: " (:err py)))
      (bind models (json/parse-string (str/trim (:out py))))
      (assert! (sequential? models) "fetch_top_models prints JSON array")
      (assert! (pos? (count models)) "fetch_top_models non-empty")
      (assert! (some #(= expected-id %) models)
               (str "fetch_top_models includes slug-derived id " expected-id " got " (pr-str models))))))

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
                    (swap! events conj raw))))
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
    (bind base-url     (str "http://127.0.0.1:" server-port))

    ;; genesis ~1 month ago so we're at epoch 1+
    (bind genesis-ms   (str (- (System/currentTimeMillis) (* 35 24 60 60 1000))))

    (bind root          (project-root))
    (test-live-slug-model-council! root)
    (bind repo-a (make-git-repository! tmp-dir "repo-a" "alice"
                                       "alice@example.test" "alice.txt"))
    (bind repo-b (make-git-repository! tmp-dir "repo-b" "bob"
                                       "bob@example.test" "bob.txt"))
    (bind repositories-json
      (json/generate-string
        [(update repo-a :refs vec) (update repo-b :refs vec)]))
    (bind contributors-json
      (json/generate-string
        {"alice" ["alice@example.test"]
         "bob"   ["bob@example.test"]}))
    (bind server-env
      {"SESSION_SECRET"       "test-secret"
       "GENESIS_MS"           genesis-ms
       "JSONL_PATH"           jsonl-path
       "GIT_MIRROR_DIR"       (str tmp-dir "/mirrors")
       "REPOSITORIES_JSON"    repositories-json
       "CONTRIBUTORS_JSON"    contributors-json
       "OPENROUTER_API_KEY"   "mock-key"
       "GITHUB_CLIENT_ID"     "mock-gh-id"
       "GITHUB_CLIENT_SECRET" "mock-gh-secret"
       "PORT"                 (str server-port)
       "DISABLE_EPOCH_LOOP"   "1"
       "ALLOW_TEST_TRIGGERS"  "1"
       "OPENROUTER_BASE_URL"  (str "http://127.0.0.1:" or-port)
       "SLUG_MODEL_RANK_PARENT" ""
       "PUBLIC_BASE_URL"      (str "http://127.0.0.1:" server-port)
       "PATH"                 (get (into {} (System/getenv)) "PATH" "")})

    (bind !server  (atom nil))
    (bind !server2 (atom nil))
    (bind !or-mock (atom nil))

    (try
      (letlocals
        ;; 1. start mock model server; Git discovery uses local bare remotes
        (println "starting mock OpenRouter server and local Git remotes…")
        (bind or-mock (start-mock-openrouter or-port))
        (reset! !or-mock or-mock)
        (assert! (some? (:stop-fn or-mock)) "mock OpenRouter started")
        (assert! (fs/exists? (:url repo-a)) "first bare Git remote exists")
        (assert! (fs/exists? (:url repo-b)) "second bare Git remote exists")

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

        ;; 9. watch UI exposes progress, controls, readiness, and live SSE
        (println "\nchecking /watch UI…")
        (bind watch-html (slurp (str base-url "/watch")))
        (assert! (str/includes? watch-html "role=\"progressbar\"")
                 "watch page has progress bar")
        (assert! (str/includes? watch-html "id=\"play\"")
                 "watch page has play control")
        (assert! (str/includes? watch-html "id=\"pause\"")
                 "watch page has pause control")
        (assert! (str/includes? watch-html "OpenRouter configured")
                 "watch page reports council readiness")
        (bind status-resp (get-json base-url "/api/status"))
        (assert! (true? (:openrouter_configured status-resp))
                 "status API reports OpenRouter configuration")

        ;; 10. SSE connects and sends initial event
        (println "\nchecking /sse initial event…")
        (bind sse-events (read-sse-events (str base-url "/sse") 1 5000))
        (assert! (= 1 (count sse-events))             "received 1 SSE event")
        (assert! (not (str/blank? (first sse-events)))
                 "initial SSE event contains executable audit data")

        ;; 11. POST /test/emit — full ranking pipeline hits mocks
        (println "\ntriggering /test/emit (epoch 1)…")
        (bind emit-resp (post-json! base-url "/test/emit"))
        (assert! (= "emission" (:type emit-resp))     "emit response type is emission")
        (assert! (= 1 (:epoch emit-resp))             "emit is epoch 1 after seeded epoch 0")
        (assert! (pos? (count (:distributions emit-resp))) "emit has distributions")
        (assert! (string? (:discovery_snapshot_id emit-resp))
                 "emission records discovery snapshot")
        (assert! (= 3 (count (:models_used emit-resp)))
                 "emission records all council models")

        (bind or-state @(:state or-mock))
        (assert! (pos? (:model-requests or-state))    "OpenRouter /models was called")
        (assert! (>= (:compare-requests or-state) 3) "at least 3 pairwise LLM calls (2 authors × 3 models)")

        (bind ledger2 (get-json base-url "/api/ledger"))
        (assert! (>= (count ledger2) 3)
                 "ledger has seed + discovery + emission (+ evidence)")
        (bind discovery-entry
          (first (filter #(= "gitdiscovery" (:type %)) ledger2)))
        (assert! (some? discovery-entry)
                 "discovery is persisted before emission")
        (assert! (= 2 (count (:repositories discovery-entry)))
                 "discovery records both repositories")
        (assert! (= 2 (count (:commits discovery-entry)))
                 "discovery admits one contribution from each repository")
        (bind evidence-kinds
          (set (keep :kind (filter #(= "evidence" (:type %)) ledger2))))
        (assert! (contains? evidence-kinds "git.commit")
                 "evidence includes git.commit")
        (assert! (contains? evidence-kinds "comparison.input")
                 "evidence includes comparison.input")
        (assert! (contains? evidence-kinds "llm.judgment")
                 "evidence includes llm.judgment")
        (bind rank-after (get-json base-url "/api/ranking"))
        (assert! (= 1 (:epoch rank-after))           "latest ranking is epoch 1")

        ;; 11b. HTML evidence indexes are crawlable
        (println "\nchecking HTML evidence indexes…")
        (bind epochs-html (slurp (str base-url "/epochs")))
        (assert! (str/includes? epochs-html "/epochs/1")
                 "epochs index links epoch 1")
        (bind epoch-html (slurp (str base-url "/epochs/1")))
        (assert! (str/includes? epoch-html "/commits/")
                 "epoch page links commits")
        (assert! (str/includes? epoch-html "/comparisons/")
                 "epoch page links comparisons")
        (assert! (str/includes? epoch-html "/judgments/")
                 "epoch page links judgments")
        (bind commit-href
          (second (re-find #"/commits/(c_[a-f0-9]+)" epoch-html)))
        (assert! (some? commit-href) "found a commit id on epoch page")
        (bind commit-html (slurp (str base-url "/commits/" commit-href)))
        (assert! (str/includes? commit-html "download patch")
                 "commit page offers patch download")
        (bind patch-bytes
          (let [tmp (doto (java.io.File/createTempFile "patch" ".bin") .deleteOnExit)
                r   @(p/process ["curl" "-sS" "-o" (.getAbsolutePath tmp)
                                 (str base-url "/commits/" commit-href "/patch")]
                                {:out :string :err :string})]
            (assert! (zero? (:exit r)) "patch download succeeds")
            (slurp tmp)))
        (assert! (pos? (count patch-bytes)) "patch download is non-empty")

        ;; 12. kill and restart — prove replay determinism + no duplicate LLM calls
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
        (assert! (= (count ledger2) (count replayed-ledger))
                 "ledger entry count unchanged after replay")
        (bind replayed-rank   (get-json base-url "/api/ranking"))
        (assert! (= (get-in rank-after [:ranking :alice])
                    (get-in replayed-rank [:ranking :alice]))
                 "alice's rank score (epoch 1) identical after replay")
        (bind epoch-html2 (slurp (str base-url "/epochs/1")))
        (assert! (str/includes? epoch-html2 "/judgments/")
                 "epoch HTML evidence still linked after restart"))

      (finally
        (when-some [s @!server]
          (println "\nkilling server…")
          (.destroyForcibly (:proc s))
          (deref s))
        (when-some [s @!server2]
          (.destroyForcibly (:proc s))
          (deref s))
        (when-some [m @!or-mock] ((:stop-fn m)))
        (fs/delete-tree tmp-dir)))

    (let [{:keys [pass fail]} @counts]
      (if (zero? fail)
        (println (str "\n" ansi-green "━━━ " pass " checks passed ━━━" ansi-reset "\n"))
        (do (println (str "\n" ansi-red "━━━ " fail " FAILED / " pass " passed ━━━" ansi-reset "\n"))
            (System/exit 1))))))

(integration)
