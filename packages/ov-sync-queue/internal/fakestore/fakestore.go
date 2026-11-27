// Package fakestore is an in-memory syncer.Store for tests, with a log of
// every call and failures that can be injected per key.
package fakestore

import (
	"context"
	"errors"
	"maps"
	"sync"

	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/syncer"
)

// Call is one logged request.
type Call struct {
	Op, Key string
	Body    []byte
}

// Store holds objects by key.
type Store struct {
	mu      sync.Mutex
	Objects map[string][]byte
	Calls   []Call
	// FailPut and FailDelete make a call on that key fail.
	FailPut, FailDelete map[string]bool
}

// New returns an empty store.
func New() *Store {
	return &Store{Objects: map[string][]byte{}, FailPut: map[string]bool{}, FailDelete: map[string]bool{}}
}

var errInjected = errors.New("injected failure")

func (s *Store) log(op, key string, body []byte) {
	s.Calls = append(s.Calls, Call{Op: op, Key: key, Body: append([]byte(nil), body...)})
}

// Get returns a copy of key's body.
func (s *Store) Get(_ context.Context, key string) ([]byte, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.log("GET", key, nil)
	body, ok := s.Objects[key]
	if !ok {
		return nil, syncer.ErrNotFound
	}
	return append([]byte(nil), body...), nil
}

// Put stores body at key unless FailPut names it.
func (s *Store) Put(_ context.Context, key string, body []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.log("PUT", key, body)
	if s.FailPut[key] {
		return errInjected
	}
	s.Objects[key] = append([]byte(nil), body...)
	return nil
}

// Delete removes key unless FailDelete names it.
func (s *Store) Delete(_ context.Context, key string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.log("DELETE", key, nil)
	if s.FailDelete[key] {
		return errInjected
	}
	delete(s.Objects, key)
	return nil
}

// Writes returns the PUT and DELETE calls, then clears the log.
func (s *Store) Writes() []Call {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []Call
	for _, c := range s.Calls {
		if c.Op != "GET" {
			out = append(out, c)
		}
	}
	s.Calls = nil
	return out
}

// Snapshot returns a copy of the objects.
func (s *Store) Snapshot() map[string][]byte {
	s.mu.Lock()
	defer s.mu.Unlock()
	return maps.Clone(s.Objects)
}
