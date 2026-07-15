package taskstore

import (
	"context"
	"strings"
	"time"
)

func (s *SQLStore) UseNonce(
	ctx context.Context,
	clientID string,
	nonce string,
	expiresAt time.Time,
) (bool, error) {
	clientID = strings.TrimSpace(clientID)
	nonce = strings.TrimSpace(nonce)
	if clientID == "" || nonce == "" {
		return false, nil
	}
	now := time.Now().UTC()
	if expiresAt.IsZero() {
		expiresAt = now.Add(5 * time.Minute)
	}
	_, _ = s.db.ExecContext(
		ctx,
		`DELETE FROM gateway_hmac_nonces WHERE expires_at < ? LIMIT 1000`,
		now,
	)
	result, err := s.db.ExecContext(
		ctx,
		`INSERT IGNORE INTO gateway_hmac_nonces (
		   client_id, nonce, expires_at, created_at
		 ) VALUES (?, ?, ?, ?)`,
		clientID,
		nonce,
		expiresAt,
		now,
	)
	if err != nil {
		return false, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return false, err
	}
	return affected > 0, nil
}
