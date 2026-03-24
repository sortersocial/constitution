#!/usr/bin/env bb
(require '[babashka.process :as p]
         '[babashka.fs :as fs])

(def root (str (fs/parent (fs/absolutize (fs/file *file*)))))

(let [result @(p/process ["bb" (str root "/tests/integration.clj")]
                         {:inherit true :dir root})]
  (System/exit (:exit result)))
