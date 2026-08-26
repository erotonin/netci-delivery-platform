package main

import (
    "encoding/json"
    "net/http"
    "net/http/httptest"
    "testing"
)

func TestHealth(t *testing.T) {
    request := httptest.NewRequest(http.MethodGet, "/healthz", nil)
    response := httptest.NewRecorder()

    newHandler("v-test").ServeHTTP(response, request)

    if response.Code != http.StatusOK {
        t.Fatalf("status = %d, want %d", response.Code, http.StatusOK)
    }
    var body map[string]string
    if err := json.NewDecoder(response.Body).Decode(&body); err != nil {
        t.Fatalf("decode response: %v", err)
    }
    if body["status"] != "ok" || body["version"] != "v-test" {
        t.Fatalf("unexpected response: %#v", body)
    }
}

func TestUnknownPathIsNotFound(t *testing.T) {
    request := httptest.NewRequest(http.MethodGet, "/missing", nil)
    response := httptest.NewRecorder()

    newHandler("v-test").ServeHTTP(response, request)

    if response.Code != http.StatusNotFound {
        t.Fatalf("status = %d, want %d", response.Code, http.StatusNotFound)
    }
}
