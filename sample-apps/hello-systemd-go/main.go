package main

import (
    "encoding/json"
    "log"
    "net/http"
    "os"
)

var version = "dev"

func main() {
    if runtimeVersion := os.Getenv("APP_VERSION"); runtimeVersion != "" {
        version = runtimeVersion
    }
    mux := http.NewServeMux()
    mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
        w.Header().Set("Content-Type", "application/json")
        _ = json.NewEncoder(w).Encode(map[string]string{"status": "ok", "version": version})
    })
    mux.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
        w.Header().Set("Content-Type", "application/json")
        _ = json.NewEncoder(w).Encode(map[string]string{"service": "hello-systemd", "version": version})
    })
    log.Printf("hello-systemd listening on :18080 version=%s", version)
    log.Fatal(http.ListenAndServe(":18080", mux))
}
