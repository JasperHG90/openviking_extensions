// Package s3store is the syncer.Store over S3, through minio-go: the client
// ov-sync-consumer already uses against SeaweedFS.
package s3store

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/url"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"

	"github.com/JasperHG90/openviking_extensions/packages/ov-sync-queue/internal/syncer"
)

// Store reads and writes keys in one bucket.
type Store struct {
	client *minio.Client
	bucket string
}

// Credentials is the standard chain: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
// and AWS_SESSION_TOKEN; then MINIO_ROOT_USER and MINIO_ROOT_PASSWORD; then
// the AWS shared credentials file, honouring AWS_PROFILE.
func Credentials() *credentials.Credentials {
	return credentials.NewChainCredentials([]credentials.Provider{
		&credentials.EnvAWS{},
		&credentials.EnvMinio{},
		&credentials.FileAWSCredentials{},
	})
}

// New connects to endpoint (with its http:// or https:// scheme).
func New(endpoint, region, bucket string, creds *credentials.Credentials) (*Store, error) {
	u, err := url.Parse(endpoint)
	if err != nil || u.Host == "" {
		return nil, fmt.Errorf("endpoint %q is not a URL", endpoint)
	}
	client, err := minio.New(u.Host, &minio.Options{
		Creds:  creds,
		Secure: u.Scheme == "https",
		Region: region,
	})
	if err != nil {
		return nil, err
	}
	return &Store{client: client, bucket: bucket}, nil
}

// Get returns a key's body, or syncer.ErrNotFound.
func (s *Store) Get(ctx context.Context, key string) ([]byte, error) {
	obj, err := s.client.GetObject(ctx, s.bucket, key, minio.GetObjectOptions{})
	if err != nil {
		return nil, notFound(err)
	}
	defer obj.Close()
	body, err := io.ReadAll(obj)
	if err != nil {
		return nil, notFound(err)
	}
	return body, nil
}

// Put writes body to key in one PUT.
func (s *Store) Put(ctx context.Context, key string, body []byte) error {
	_, err := s.client.PutObject(ctx, s.bucket, key, bytes.NewReader(body), int64(len(body)),
		minio.PutObjectOptions{ContentType: "application/octet-stream", DisableMultipart: true})
	return err
}

// Delete removes key. Removing a missing key succeeds, as in S3.
func (s *Store) Delete(ctx context.Context, key string) error {
	return s.client.RemoveObject(ctx, s.bucket, key, minio.RemoveObjectOptions{})
}

func notFound(err error) error {
	if minio.ToErrorResponse(err).Code == minio.NoSuchKey {
		return syncer.ErrNotFound
	}
	return err
}
