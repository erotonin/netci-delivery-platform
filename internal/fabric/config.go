package fabric

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
)

// Config of the fabric.
//
//	{"namespace": "netci-agents", "serviceAccount": "netci-sandbox", "audience": "netci-fabric",
//	 "bootstrapImage": "...netci@sha256:...", "fabricUrl": "http://netci-fabric.netci-system.svc:8080",
//	 "pools": [{"name": "standard", "labels": ["linux"], "image": "...inbound-agent...", "warm": 2, "max": 10,
//	            "cpu": "1", "memory": "2Gi", "disk": "8Gi", "userNamespace": true}],
//	 "cells": {"cell-b": "<sha256 of cell-b's token>"}}
type Config struct {
	Namespace      string            `json:"namespace"`
	ServiceAccount string            `json:"serviceAccount"`
	Audience       string            `json:"audience"`
	BootstrapImage string            `json:"bootstrapImage"`
	FabricURL      string            `json:"fabricUrl"`
	PullSecrets    []string          `json:"pullSecrets,omitempty"`
	Pools          []Pool            `json:"pools"`
	Cells          map[string]string `json:"cells"`
}

// LoadConfig reads and checks the configuration; anything doubtful is an error at start.
func LoadConfig(path string) (*Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var c Config
	if err := dec.Decode(&c); err != nil {
		return nil, fmt.Errorf("fabric config %s: %w", path, err)
	}
	return &c, c.Validate()
}

// Validate checks the configuration.
func (c *Config) Validate() error {
	switch {
	case c.Namespace == "" || c.ServiceAccount == "" || c.Audience == "":
		return errors.New("namespace, serviceAccount and audience are required")
	case !strings.Contains(c.BootstrapImage, "@sha256:"):
		return errors.New("bootstrapImage must be pinned by digest")
	case !strings.HasPrefix(c.FabricURL, "http://") && !strings.HasPrefix(c.FabricURL, "https://"):
		return errors.New("fabricUrl must be http(s)")
	case len(c.Pools) == 0:
		return errors.New("no pools: the fabric would serve nothing")
	case len(c.Cells) == 0:
		return errors.New("no cells: nobody could claim")
	}
	seen := map[string]bool{}
	for _, p := range c.Pools {
		if err := p.validate(); err != nil {
			return err
		}
		if seen[p.Name] {
			return fmt.Errorf("pool %s is defined twice", p.Name)
		}
		seen[p.Name] = true
	}
	hashes := map[string]bool{}
	for cell, h := range c.Cells {
		if b, err := hex.DecodeString(h); err != nil || len(b) != sha256.Size {
			return fmt.Errorf("cell %s: token hash must be a SHA-256 in hex", cell)
		}
		if hashes[h] {
			return fmt.Errorf("two cells share a token")
		}
		hashes[h] = true
	}
	return nil
}

// PoolMap indexes the pools by name.
func (c *Config) PoolMap() map[string]Pool {
	m := map[string]Pool{}
	for _, p := range c.Pools {
		m[p.Name] = p
	}
	return m
}

// Settings are the pod settings shared by every pool.
func (c *Config) Settings() PodSettings {
	return PodSettings{Namespace: c.Namespace, ServiceAccount: c.ServiceAccount, BootstrapImage: c.BootstrapImage,
		FabricURL: c.FabricURL, Audience: c.Audience, PullSecrets: c.PullSecrets}
}
