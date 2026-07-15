package taskstore

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"time"

	"ironagent/go-gateway/internal/core"
)

func (s *SQLStore) GetConversationRoute(
	ctx context.Context,
	clientID string,
	userID string,
	conversationID string,
) (core.ConversationRouteRecord, bool, error) {
	clientID = normalizeConversationRouteClientID(clientID)
	userID = strings.TrimSpace(userID)
	conversationID = strings.TrimSpace(conversationID)
	if userID == "" || conversationID == "" {
		return core.ConversationRouteRecord{}, false, nil
	}
	row := s.db.QueryRowContext(
		ctx,
		`SELECT client_id, user_id, conversation_id, assistant_id, created_at, updated_at
		 FROM gateway_conversation_routes
		 WHERE client_id = ? AND user_id = ? AND conversation_id = ?`,
		clientID,
		userID,
		conversationID,
	)
	record, err := scanConversationRouteRecord(row)
	if err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return core.ConversationRouteRecord{}, false, nil
		}
		return core.ConversationRouteRecord{}, false, err
	}
	return record, true, nil
}

func (s *SQLStore) BindConversationRoute(
	ctx context.Context,
	record core.ConversationRouteRecord,
) (core.ConversationRouteRecord, bool, error) {
	record.ClientID = normalizeConversationRouteClientID(record.ClientID)
	record.UserID = strings.TrimSpace(record.UserID)
	record.ConversationID = strings.TrimSpace(record.ConversationID)
	record.AssistantID = strings.TrimSpace(record.AssistantID)
	if record.UserID == "" || record.ConversationID == "" || record.AssistantID == "" {
		return core.ConversationRouteRecord{}, false, nil
	}
	now := time.Now().UTC()
	if record.CreatedAt.IsZero() {
		record.CreatedAt = now
	}
	record.UpdatedAt = now

	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return core.ConversationRouteRecord{}, false, err
	}
	defer tx.Rollback()

	_, err = tx.ExecContext(
		ctx,
		`INSERT INTO gateway_conversation_routes (
		   client_id, user_id, conversation_id, assistant_id, created_at, updated_at
		 ) VALUES (?, ?, ?, ?, ?, ?)
		 ON DUPLICATE KEY UPDATE updated_at = VALUES(updated_at)`,
		record.ClientID,
		record.UserID,
		record.ConversationID,
		record.AssistantID,
		record.CreatedAt,
		record.UpdatedAt,
	)
	if err != nil {
		return core.ConversationRouteRecord{}, false, err
	}

	row := tx.QueryRowContext(
		ctx,
		`SELECT client_id, user_id, conversation_id, assistant_id, created_at, updated_at
		 FROM gateway_conversation_routes
		 WHERE client_id = ? AND user_id = ? AND conversation_id = ?
		 FOR UPDATE`,
		record.ClientID,
		record.UserID,
		record.ConversationID,
	)
	stored, err := scanConversationRouteRecord(row)
	if err != nil {
		return core.ConversationRouteRecord{}, false, err
	}
	if stored.AssistantID != record.AssistantID {
		return stored, true, &core.ValidationError{
			Message: fmt.Sprintf(
				"conversation_id %q is already bound to assistant %q",
				record.ConversationID,
				stored.AssistantID,
			),
		}
	}
	if err := tx.Commit(); err != nil {
		return core.ConversationRouteRecord{}, false, err
	}
	replayed := !stored.CreatedAt.Equal(record.CreatedAt)
	return stored, replayed, nil
}

type conversationRouteScanner interface {
	Scan(dest ...any) error
}

func scanConversationRouteRecord(row conversationRouteScanner) (core.ConversationRouteRecord, error) {
	var record core.ConversationRouteRecord
	if err := row.Scan(
		&record.ClientID,
		&record.UserID,
		&record.ConversationID,
		&record.AssistantID,
		&record.CreatedAt,
		&record.UpdatedAt,
	); err != nil {
		return core.ConversationRouteRecord{}, err
	}
	return record, nil
}

func normalizeConversationRouteClientID(clientID string) string {
	clientID = strings.TrimSpace(clientID)
	if clientID == "" {
		return "default"
	}
	return clientID
}
