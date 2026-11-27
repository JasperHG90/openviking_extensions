// Command pipeline serves the two things e2e/pipeline.sh fakes around the
// real ov-sync pipeline: OpenViking, and the Nomad OIDC issuer whose workload
// token ov-sync-consumer trades with SeaweedFS's STS for S3 credentials.
//
// The fake OpenViking (FAKE_OV_ADDR) keeps files and folders in memory and
// answers the calls ov-sync-consumer makes: content/write, temp_upload plus
// resources for images, DELETE fs, and fs/ls, which lists OpenViking's
// .abstract.md and .overview.md in every folder. Anything else is logged as
// UNEXPECTED and refused. GET /_files returns every file it holds, uri to
// content, with content that is not UTF-8 shown as sha256:<hex>; GET /_dirs
// every folder; GET /_calls every call it took.
//
// The issuer (ISSUER_ADDR, public URL ISSUER) serves OIDC discovery and a
// JWKS, and writes a token to TOKEN_FILE shaped like the one Nomad gives the
// ov-sync-consumer job: audience seaweedfs, nomad_job_id ov-sync-consumer and
// a global:default:ov-sync-consumer:... subject.
package main

import (
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"math/big"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

func main() {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		log.Fatal(err)
	}
	issuer := os.Getenv("ISSUER")
	token, err := workloadToken(key, issuer, time.Now())
	if err != nil {
		log.Fatal(err)
	}
	if err := os.WriteFile(os.Getenv("TOKEN_FILE"), []byte(token), 0o644); err != nil {
		log.Fatal(err)
	}
	go func() { log.Fatal(http.ListenAndServe(os.Getenv("ISSUER_ADDR"), issuerHandler(key, issuer))) }()
	ov := &fakeOV{files: map[string][]byte{}, dirs: map[string]bool{}, temp: map[string][]byte{}}
	log.Fatal(http.ListenAndServe(os.Getenv("FAKE_OV_ADDR"), ov))
}

const keyID = "nomad-e2e"

func b64(b []byte) string { return base64.RawURLEncoding.EncodeToString(b) }

func workloadToken(key *rsa.PrivateKey, issuer string, now time.Time) (string, error) {
	header, _ := json.Marshal(map[string]string{"alg": "RS256", "typ": "JWT", "kid": keyID})
	claims, _ := json.Marshal(map[string]any{
		"iss":             issuer,
		"aud":             []string{"seaweedfs"},
		"sub":             "global:default:ov-sync-consumer:consumer:consumer:seaweedfs",
		"nomad_namespace": "default",
		"nomad_job_id":    "ov-sync-consumer",
		"nomad_task":      "consumer",
		"iat":             now.Unix(),
		"nbf":             now.Unix(),
		"exp":             now.Add(time.Hour).Unix(),
	})
	signing := b64(header) + "." + b64(claims)
	sum := sha256.Sum256([]byte(signing))
	sig, err := rsa.SignPKCS1v15(rand.Reader, key, crypto.SHA256, sum[:])
	if err != nil {
		return "", err
	}
	return signing + "." + b64(sig), nil
}

func issuerHandler(key *rsa.PrivateKey, issuer string) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/.well-known/openid-configuration", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"issuer":                                issuer,
			"jwks_uri":                              issuer + "/.well-known/jwks.json",
			"id_token_signing_alg_values_supported": []string{"RS256"},
		})
	})
	mux.HandleFunc("/.well-known/jwks.json", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"keys": []map[string]string{{
			"kty": "RSA", "kid": keyID, "use": "sig", "alg": "RS256",
			"n": b64(key.N.Bytes()), "e": b64(big.NewInt(int64(key.E)).Bytes()),
		}}})
	})
	return mux
}

type fakeOV struct {
	mu    sync.Mutex
	files map[string][]byte
	// dirs holds every folder a write created. As in OpenViking, a folder
	// stays when its last file goes, until a recursive delete removes it.
	dirs  map[string]bool
	temp  map[string][]byte
	calls []string
}

func parent(uri string) string { return uri[:max(strings.LastIndex(uri, "/"), 0)] }

func (f *fakeOV) mkdirs(uri string) {
	for d := parent(uri); strings.Contains(d, "://"); d = parent(d) {
		f.dirs[d] = true
	}
}

func (f *fakeOV) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	ok := func(result any) {
		_ = json.NewEncoder(w).Encode(map[string]any{"status": "ok", "result": result})
	}
	refuse := func(status int) { http.Error(w, `{"status":"error"}`, status) }
	q := r.URL.Query()
	switch {
	case r.Method == http.MethodGet && r.URL.Path == "/_files":
		out := map[string]string{}
		for k, v := range f.files {
			if utf8.Valid(v) {
				out[k] = string(v)
			} else {
				sum := sha256.Sum256(v)
				out[k] = "sha256:" + hex.EncodeToString(sum[:])
			}
		}
		_ = json.NewEncoder(w).Encode(out)
	case r.Method == http.MethodGet && r.URL.Path == "/_dirs":
		_ = json.NewEncoder(w).Encode(f.dirs)
	case r.Method == http.MethodGet && r.URL.Path == "/_calls":
		_ = json.NewEncoder(w).Encode(f.calls)
	case r.Header.Get("Authorization") != "Bearer ov-token":
		refuse(http.StatusUnauthorized)
	case r.Method == http.MethodPost && r.URL.Path == "/api/v1/content/write":
		var body struct {
			URI     string `json:"uri"`
			Content string `json:"content"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			refuse(http.StatusBadRequest)
			return
		}
		f.calls = append(f.calls, "WRITE "+body.URI)
		f.files[body.URI] = []byte(body.Content)
		f.mkdirs(body.URI)
		ok(nil)
	case r.Method == http.MethodPost && r.URL.Path == "/api/v1/resources/temp_upload":
		file, _, err := r.FormFile("file")
		if err != nil {
			refuse(http.StatusBadRequest)
			return
		}
		raw, err := io.ReadAll(file)
		if err != nil {
			refuse(http.StatusBadRequest)
			return
		}
		id := fmt.Sprintf("temp-%d", len(f.temp)+1)
		f.temp[id] = raw
		ok(map[string]string{"temp_file_id": id})
	case r.Method == http.MethodPost && r.URL.Path == "/api/v1/resources":
		var body struct {
			TempID string `json:"temp_file_id"`
			To     string `json:"to"`
			Name   string `json:"source_name"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil || f.temp[body.TempID] == nil || body.To == "" {
			refuse(http.StatusBadRequest)
			return
		}
		folder := strings.TrimSuffix(body.To, "/")
		if _, isFile := f.files[folder]; isFile {
			// OpenViking cannot add a resource where a plain file sits.
			refuse(http.StatusConflict)
			return
		}
		f.calls = append(f.calls, "UPLOAD "+folder+"/"+body.Name)
		// The add replaces whatever the folder held.
		f.removeTree(folder)
		f.files[folder+"/"+body.Name] = f.temp[body.TempID]
		f.dirs[folder] = true
		f.mkdirs(folder)
		delete(f.temp, body.TempID)
		ok(nil)
	case r.Method == http.MethodDelete && r.URL.Path == "/api/v1/fs":
		uri := strings.TrimSuffix(q.Get("uri"), "/")
		f.calls = append(f.calls, "DELETE "+uri)
		if _, isFile := f.files[uri]; isFile {
			delete(f.files, uri)
		} else if f.dirs[uri] && q.Get("recursive") == "true" {
			f.removeTree(uri)
			delete(f.dirs, uri)
		} else if f.dirs[uri] {
			refuse(http.StatusPreconditionFailed)
			return
		} else {
			refuse(http.StatusNotFound)
			return
		}
		ok(nil)
	case r.Method == http.MethodGet && r.URL.Path == "/api/v1/fs/ls":
		uri := strings.TrimSuffix(q.Get("uri"), "/")
		if !f.dirs[uri] {
			refuse(http.StatusNotFound)
			return
		}
		// OpenViking keeps a summary pair in every folder, so the consumer
		// must look past them to see that a folder is empty.
		entries := []map[string]any{
			{"uri": uri + "/.abstract.md", "isDir": false},
			{"uri": uri + "/.overview.md", "isDir": false},
		}
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
		ok(entries)
	default:
		f.calls = append(f.calls, "UNEXPECTED "+r.Method+" "+r.URL.Path)
		refuse(http.StatusNotImplemented)
	}
}

// removeTree drops every file and folder under uri, keeping uri itself.
func (f *fakeOV) removeTree(uri string) {
	for k := range f.files {
		if strings.HasPrefix(k, uri+"/") {
			delete(f.files, k)
		}
	}
	for d := range f.dirs {
		if strings.HasPrefix(d, uri+"/") {
			delete(f.dirs, d)
		}
	}
}
