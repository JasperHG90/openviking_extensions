package s3store

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"

	"github.com/minio/minio-go/v7/pkg/credentials"

	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/syncer"
)

// fakeS3 answers path-style GET, PUT and DELETE on one bucket, and refuses
// everything else, like a credential with no List.
type fakeS3 struct {
	mu      sync.Mutex
	objects map[string][]byte
	methods []string
}

func (f *fakeS3) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	key, ok := strings.CutPrefix(r.URL.Path, "/ov-sync/")
	if !ok || key == "" || r.URL.RawQuery != "" && r.Method != http.MethodPut {
		http.Error(w, "<Error><Code>AccessDenied</Code></Error>", http.StatusForbidden)
		return
	}
	f.methods = append(f.methods, r.Method)
	switch r.Method {
	case http.MethodPut:
		body, err := readBody(r)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		f.objects[key] = body
		w.Header().Set("ETag", `"x"`)
	case http.MethodGet:
		body, ok := f.objects[key]
		if !ok {
			w.Header().Set("Content-Type", "application/xml")
			w.WriteHeader(http.StatusNotFound)
			_, _ = io.WriteString(w, "<Error><Code>NoSuchKey</Code><Message>missing</Message><Key>"+key+"</Key></Error>")
			return
		}
		w.Header().Set("Content-Length", strconv.Itoa(len(body)))
		w.Header().Set("Last-Modified", "Mon, 02 Jan 2006 15:04:05 GMT")
		w.Header().Set("ETag", `"x"`)
		_, _ = w.Write(body)
	case http.MethodDelete:
		delete(f.objects, key)
		w.WriteHeader(http.StatusNoContent)
	default:
		http.Error(w, "<Error><Code>AccessDenied</Code></Error>", http.StatusForbidden)
	}
}

// readBody decodes an aws-chunked body when the client streamed one.
func readBody(r *http.Request) ([]byte, error) {
	if !strings.HasPrefix(r.Header.Get("X-Amz-Content-Sha256"), "STREAMING-") {
		return io.ReadAll(r.Body)
	}
	var out bytes.Buffer
	br := bufio.NewReader(r.Body)
	for {
		line, err := br.ReadString('\n')
		if err != nil {
			return nil, err
		}
		size, err := strconv.ParseInt(strings.TrimSpace(strings.SplitN(line, ";", 2)[0]), 16, 64)
		if err != nil {
			return nil, err
		}
		if size == 0 {
			return out.Bytes(), nil
		}
		if _, err := io.CopyN(&out, br, size); err != nil {
			return nil, err
		}
		if _, err := br.Discard(2); err != nil {
			return nil, err
		}
	}
}

func TestS3Store(t *testing.T) {
	fake := &fakeS3{objects: map[string][]byte{}}
	srv := httptest.NewServer(fake)
	defer srv.Close()
	store, err := New(srv.URL, "us-east-1", "ov-sync", credentials.NewStaticV4("id", "secret", ""))
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()

	if _, err := store.Get(ctx, "o/n/_state/ovsq-local.json"); !errors.Is(err, syncer.ErrNotFound) {
		t.Fatalf("missing key: want ErrNotFound, got %v", err)
	}
	body := bytes.Repeat([]byte("abc\n"), 1<<20) // 4 MiB, one PUT
	if err := store.Put(ctx, "o/n/docs/a.md", body); err != nil {
		t.Fatal(err)
	}
	got, err := store.Get(ctx, "o/n/docs/a.md")
	if err != nil || !bytes.Equal(got, body) {
		t.Fatalf("round trip: err %v, %d bytes back of %d", err, len(got), len(body))
	}
	if err := store.Delete(ctx, "o/n/docs/a.md"); err != nil {
		t.Fatal(err)
	}
	if _, ok := fake.objects["o/n/docs/a.md"]; ok {
		t.Fatal("delete left the object")
	}
	for _, m := range fake.methods {
		if m == http.MethodPost {
			t.Fatal("client started a multipart upload")
		}
	}
}

func TestNewRefusesBadEndpoint(t *testing.T) {
	if _, err := New("not a url", "us-east-1", "b", credentials.NewStaticV4("a", "b", "")); err == nil {
		t.Fatal("want an error")
	}
}
