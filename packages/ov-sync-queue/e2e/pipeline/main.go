//go:build ignore

// ovsq-pipeline-consumer runs the real ov-sync-consumer against a fake
// OpenViking, for e2e/pipeline.sh. pipeline.sh copies it into a copy of the
// ov-sync-consumer module from localstack, so it can use that module's
// internal packages. It differs from the real consumer's main in three ways:
// it uses static S3 keys instead of the Nomad STS exchange, it never runs the
// catch-up pass (so whatever reaches OpenViking came through an event), and
// it serves the fake OpenViking itself.
//
// The fake keeps files and folders in memory and answers the three calls the
// consumer makes for text files: content/write, DELETE fs and fs/ls. Anything
// else, such as an image upload, is logged as UNEXPECTED and refused. GET
// /_files returns every file it holds (uri to content), GET /_dirs every
// folder, and GET /_calls every call it took.
package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"sort"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"
	"golang.org/x/time/rate"

	"github.com/JasperHG90/localstack/applications/ov-sync-consumer/internal/consumer"
)

type fakeOV struct {
	mu    sync.Mutex
	files map[string]string
	// dirs holds every folder a write created. As in OpenViking, a folder
	// stays when its last file goes, until a recursive delete removes it.
	dirs  map[string]bool
	calls []string
}

func (f *fakeOV) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	ok := func(result any) {
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok", "result": result})
	}
	q := r.URL.Query()
	switch {
	case r.Method == http.MethodGet && r.URL.Path == "/_files":
		_ = json.NewEncoder(w).Encode(f.files)
	case r.Method == http.MethodGet && r.URL.Path == "/_dirs":
		_ = json.NewEncoder(w).Encode(f.dirs)
	case r.Method == http.MethodGet && r.URL.Path == "/_calls":
		_ = json.NewEncoder(w).Encode(f.calls)
	case r.Header.Get("Authorization") != "Bearer ov-token":
		http.Error(w, `{"status":"error"}`, http.StatusUnauthorized)
	case r.Method == http.MethodPost && r.URL.Path == "/api/v1/content/write":
		var body struct {
			URI     string `json:"uri"`
			Content string `json:"content"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			http.Error(w, `{"status":"error"}`, http.StatusBadRequest)
			return
		}
		f.calls = append(f.calls, "WRITE "+body.URI)
		f.files[body.URI] = body.Content
		for d := parent(body.URI); strings.Contains(d, "://") && !strings.HasSuffix(d, ":/"); d = parent(d) {
			f.dirs[d] = true
		}
		ok(nil)
	case r.Method == http.MethodDelete && r.URL.Path == "/api/v1/fs":
		uri := strings.TrimSuffix(q.Get("uri"), "/")
		f.calls = append(f.calls, "DELETE "+uri)
		switch {
		case hasKey(f.files, uri):
			delete(f.files, uri)
		case f.dirs[uri] && q.Get("recursive") == "true":
			for k := range f.files {
				if strings.HasPrefix(k, uri+"/") {
					delete(f.files, k)
				}
			}
			for d := range f.dirs {
				if d == uri || strings.HasPrefix(d, uri+"/") {
					delete(f.dirs, d)
				}
			}
		case f.dirs[uri]:
			http.Error(w, `{"status":"error"}`, http.StatusPreconditionFailed)
			return
		default:
			http.Error(w, `{"status":"error"}`, http.StatusNotFound)
			return
		}
		ok(nil)
	case r.Method == http.MethodGet && r.URL.Path == "/api/v1/fs/ls":
		uri := strings.TrimSuffix(q.Get("uri"), "/")
		if !f.dirs[uri] {
			http.Error(w, `{"status":"error"}`, http.StatusNotFound)
			return
		}
		entries := []map[string]any{}
		for k := range f.files {
			if parent(k) == uri {
				entries = append(entries, map[string]any{"uri": k, "isDir": false})
			}
		}
		for d := range f.dirs {
			if parent(d) == uri {
				entries = append(entries, map[string]any{"uri": d, "isDir": true})
			}
		}
		sort.Slice(entries, func(i, j int) bool { return entries[i]["uri"].(string) < entries[j]["uri"].(string) })
		ok(entries)
	default:
		f.calls = append(f.calls, "UNEXPECTED "+r.Method+" "+r.URL.Path)
		http.Error(w, `{"status":"error"}`, http.StatusNotImplemented)
	}
}

func parent(uri string) string { return uri[:max(strings.LastIndex(uri, "/"), 0)] }

func hasKey(m map[string]string, k string) bool { _, ok := m[k]; return ok }

func main() {
	log := slog.New(slog.NewTextHandler(os.Stderr, nil))
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	if err := run(ctx, log); err != nil {
		log.Error("exiting", "err", err)
		os.Exit(1)
	}
}

func run(ctx context.Context, log *slog.Logger) error {
	ov := &fakeOV{files: map[string]string{}, dirs: map[string]bool{}}
	go func() { _ = http.ListenAndServe(os.Getenv("FAKE_OV_ADDR"), ov) }()

	mapping, err := consumer.LoadMapping(os.Getenv("OV_SYNC_MAP_FILE"))
	if err != nil {
		return err
	}
	s3, err := minio.New(os.Getenv("S3_ENDPOINT"), &minio.Options{
		Creds:  credentials.NewStaticV4(os.Getenv("S3_ACCESS_KEY"), os.Getenv("S3_SECRET_KEY"), ""),
		Region: "us-east-1",
	})
	if err != nil {
		return err
	}
	c := &consumer.Consumer{
		S3:     s3,
		Bucket: "ov-sync",
		OV: &consumer.OpenViking{
			URL:         "http://" + os.Getenv("FAKE_OV_ADDR"),
			TokenFile:   os.Getenv("OV_TOKEN_FILE"),
			HTTP:        &http.Client{Timeout: 30 * time.Second},
			MaxInFlight: 4,
		},
		Map:      mapping,
		Pool:     4,
		Limit:    rate.NewLimiter(rate.Inf, 1),
		MaxTries: 3,
		Backoff:  consumer.Backoff(500*time.Millisecond, 2*time.Second),
		AckWait:  30 * time.Second,
		Log:      log,
	}
	nc, err := nats.Connect(os.Getenv("NATS_URL"), nats.RetryOnFailedConnect(true), nats.MaxReconnects(-1))
	if err != nil {
		return err
	}
	defer nc.Close()
	js, err := jetstream.New(nc)
	if err != nil {
		return err
	}
	log.Info("consumer starting")
	return c.Run(ctx, js)
}
