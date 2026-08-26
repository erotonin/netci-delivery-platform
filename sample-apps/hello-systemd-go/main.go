package main

import (
    "encoding/json"
    "log"
    "net/http"
    "os"
    "time"
)

var version = "dev"

func newHandler(serviceVersion string) http.Handler {
    mux := http.NewServeMux()
    mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
        if r.Method != http.MethodGet {
            http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
            return
        }
        w.Header().Set("Content-Type", "application/json")
        _ = json.NewEncoder(w).Encode(map[string]string{"status": "ok", "version": serviceVersion})
    })
    mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
        if r.URL.Path != "/" {
            http.NotFound(w, r)
            return
        }
        if r.Method != http.MethodGet {
            http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
            return
        }
        w.Header().Set("Content-Type", "application/json")
        _ = json.NewEncoder(w).Encode(map[string]string{"service": "hello-systemd", "version": serviceVersion})
    })
    return mux
}

func main() {
    serviceVersion := version
    if runtimeVersion := os.Getenv("APP_VERSION"); runtimeVersion != "" {
        serviceVersion = runtimeVersion
    }
    server := &http.Server{
        Addr:              ":18080",
        Handler:           newHandler(serviceVersion),
        ReadHeaderTimeout: 5 * time.Second,
    }
    log.Printf("hello-systemd listening on :18080 version=%s", serviceVersion)
    log.Fatal(server.ListenAndServe())
}
