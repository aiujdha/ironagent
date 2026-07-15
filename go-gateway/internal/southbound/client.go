package southbound

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/textproto"
	"net/url"
	"reflect"
	"strings"
	"time"

	"ironagent/go-gateway/internal/metrics"
)

type Config struct {
	SecurityBaseURL      string
	AdminBaseURL         string
	InternalServiceToken string
	HTTPTimeout          time.Duration
}

type Client struct {
	securityBaseURL      string
	adminBaseURL         string
	internalServiceToken string
	httpClient           *http.Client
}

func NewClient(cfg Config) *Client {
	return &Client{
		securityBaseURL:      strings.TrimRight(cfg.SecurityBaseURL, "/"),
		adminBaseURL:         strings.TrimRight(cfg.AdminBaseURL, "/"),
		internalServiceToken: strings.TrimSpace(cfg.InternalServiceToken),
		httpClient: &http.Client{
			Timeout: cfg.HTTPTimeout,
		},
	}
}

func (c *Client) internalAuthHeaders() map[string]string {
	headers := map[string]string{}
	if c.internalServiceToken != "" {
		headers["X-IronAgent-Internal-Token"] = c.internalServiceToken
	}
	return headers
}

type HTTPError struct {
	StatusCode int
	Method     string
	URL        string
	Body       string
}

func (e *HTTPError) Error() string {
	return fmt.Sprintf("%s %s returned %d: %s", e.Method, e.URL, e.StatusCode, e.Body)
}

func (c *Client) GetUser(ctx context.Context, userID string) (User, error) {
	var response User
	path := fmt.Sprintf("/api/v1/users/%s", url.PathEscape(strings.TrimSpace(userID)))
	if err := c.doJSON(ctx, c.securityBaseURL, http.MethodGet, path, nil, nil, &response); err != nil {
		return User{}, err
	}
	return response, nil
}

func (c *Client) ListUserAssistants(ctx context.Context, userID string) ([]UserAssistantBinding, error) {
	var response BindingListResponse
	path := fmt.Sprintf("/api/v1/users/%s/assistants", url.PathEscape(strings.TrimSpace(userID)))
	if err := c.doJSON(ctx, c.securityBaseURL, http.MethodGet, path, nil, nil, &response); err != nil {
		return nil, err
	}
	return response.Bindings, nil
}

func (c *Client) CreateSharedObject(ctx context.Context, request SharedObjectCreateRequest) (SharedObject, error) {
	var response SharedObject
	var err error
	if strings.TrimSpace(request.IdempotencyKey) != "" {
		err = c.doJSONTransientRetry(ctx, c.securityBaseURL, http.MethodPost, "/api/v1/collaboration/objects", nil, request, &response)
	} else {
		err = c.doJSON(ctx, c.securityBaseURL, http.MethodPost, "/api/v1/collaboration/objects", nil, request, &response)
	}
	if err != nil {
		return SharedObject{}, err
	}
	return response, nil
}

func (c *Client) CreateTask(ctx context.Context, request CreateTaskRequest) (Task, error) {
	var response Task
	var err error
	if strings.TrimSpace(request.IdempotencyKey) != "" {
		err = c.doJSONTransientRetry(ctx, c.securityBaseURL, http.MethodPost, "/api/v1/collaboration/tasks", nil, request, &response)
	} else {
		err = c.doJSON(ctx, c.securityBaseURL, http.MethodPost, "/api/v1/collaboration/tasks", nil, request, &response)
	}
	if err != nil {
		return Task{}, err
	}
	return response, nil
}

func (c *Client) GetTask(ctx context.Context, taskID string) (Task, error) {
	var response Task
	path := fmt.Sprintf("/api/v1/collaboration/tasks/%s", url.PathEscape(strings.TrimSpace(taskID)))
	if err := c.doJSON(ctx, c.securityBaseURL, http.MethodGet, path, nil, nil, &response); err != nil {
		return Task{}, err
	}
	return response, nil
}

func (c *Client) GetApproval(ctx context.Context, approvalID string) (ApprovalStatus, error) {
	var response ApprovalStatus
	path := fmt.Sprintf("/api/v1/approvals/%s", url.PathEscape(strings.TrimSpace(approvalID)))
	if err := c.doJSON(ctx, c.securityBaseURL, http.MethodGet, path, nil, nil, &response); err != nil {
		return ApprovalStatus{}, err
	}
	return response, nil
}

func (c *Client) ListApprovals(
	ctx context.Context,
	status string,
	assistantID string,
	approvalScope string,
) ([]ApprovalStatus, error) {
	query := url.Values{}
	if status = strings.TrimSpace(status); status != "" {
		query.Set("status", status)
	}
	if assistantID = strings.TrimSpace(assistantID); assistantID != "" {
		query.Set("assistant_id", assistantID)
	}
	if approvalScope = strings.TrimSpace(approvalScope); approvalScope != "" {
		query.Set("approval_scope", approvalScope)
	}
	path := "/api/v1/approvals"
	if encoded := query.Encode(); encoded != "" {
		path += "?" + encoded
	}
	var response []ApprovalStatus
	if err := c.doJSON(ctx, c.securityBaseURL, http.MethodGet, path, nil, nil, &response); err != nil {
		return nil, err
	}
	return response, nil
}

func (c *Client) GetAssistant(ctx context.Context, assistantID string) (Assistant, error) {
	var response Assistant
	path := fmt.Sprintf("/api/v1/assistants/%s", url.PathEscape(strings.TrimSpace(assistantID)))
	if err := c.doJSON(ctx, c.securityBaseURL, http.MethodGet, path, nil, nil, &response); err != nil {
		return Assistant{}, err
	}
	return response, nil
}

func (c *Client) ListAssistantContext(ctx context.Context) ([]AssistantContextEntry, error) {
	var response AssistantContextResponse
	if err := c.doJSON(ctx, c.securityBaseURL, http.MethodGet, "/api/v1/control-ui/assistant-context", nil, nil, &response); err != nil {
		return nil, err
	}
	return response.Assistants, nil
}

func (c *Client) StartAssistant(
	ctx context.Context,
	assistantID string,
) (AssistantRuntimeControlResponse, error) {
	var response AssistantRuntimeControlResponse
	headers := map[string]string{
		"X-IronAgent-Internal-Token": c.internalServiceToken,
	}
	path := fmt.Sprintf("/api/v1/admin/assistants/%s/start", url.PathEscape(strings.TrimSpace(assistantID)))
	if err := c.doJSON(ctx, c.adminBaseURL, http.MethodPost, path, headers, nil, &response); err != nil {
		return AssistantRuntimeControlResponse{}, err
	}
	return response, nil
}

func (c *Client) StopAssistant(
	ctx context.Context,
	assistantID string,
) (AssistantRuntimeControlResponse, error) {
	var response AssistantRuntimeControlResponse
	headers := map[string]string{
		"X-IronAgent-Internal-Token": c.internalServiceToken,
	}
	path := fmt.Sprintf("/api/v1/admin/assistants/%s/stop", url.PathEscape(strings.TrimSpace(assistantID)))
	if err := c.doJSON(ctx, c.adminBaseURL, http.MethodPost, path, headers, nil, &response); err != nil {
		return AssistantRuntimeControlResponse{}, err
	}
	return response, nil
}

func (c *Client) ResolveControlUILaunchTicket(
	ctx context.Context,
	ticket string,
	origin string,
) (ControlUILaunchTicketResolveResponse, error) {
	var response ControlUILaunchTicketResolveResponse
	headers := c.internalAuthHeaders()
	if strings.TrimSpace(origin) != "" {
		headers["Origin"] = strings.TrimSpace(origin)
	}
	if err := c.doJSON(ctx, c.securityBaseURL, http.MethodPost, "/api/v1/control-ui/launch-tickets/resolve", headers, ControlUILaunchTicketResolveRequest{
		Ticket: strings.TrimSpace(ticket),
	}, &response); err != nil {
		return ControlUILaunchTicketResolveResponse{}, err
	}
	return response, nil
}

func (c *Client) ResolveApproval(
	ctx context.Context,
	approvalID string,
	resolution string,
) (ApprovalResolution, error) {
	var response ApprovalResolution
	headers := map[string]string{
		"X-IronAgent-Internal-Token": c.internalServiceToken,
	}
	path := fmt.Sprintf("/api/v1/admin/approvals/%s/resolve", url.PathEscape(strings.TrimSpace(approvalID)))
	if err := c.doJSON(ctx, c.adminBaseURL, http.MethodPost, path, headers, ApprovalResolutionRequest{
		Resolution: resolution,
	}, &response); err != nil {
		return ApprovalResolution{}, err
	}
	return response, nil
}

func (c *Client) CancelTask(ctx context.Context, taskID string, request CancelTaskRequest) (Task, error) {
	var response Task
	path := fmt.Sprintf("/api/v1/collaboration/tasks/%s/cancel", url.PathEscape(strings.TrimSpace(taskID)))
	if err := c.doJSON(ctx, c.securityBaseURL, http.MethodPost, path, nil, request, &response); err != nil {
		return Task{}, err
	}
	return response, nil
}

func (c *Client) ClaimTask(ctx context.Context, taskID string, assistantID string) (ClaimResponse, error) {
	var response ClaimResponse
	path := fmt.Sprintf("/api/v1/collaboration/tasks/%s/claim", url.PathEscape(strings.TrimSpace(taskID)))
	if err := c.doJSON(ctx, c.securityBaseURL, http.MethodPost, path, nil, ClaimTaskRequest{
		AssistantID: strings.TrimSpace(assistantID),
	}, &response); err != nil {
		return ClaimResponse{}, err
	}
	return response, nil
}

func (c *Client) RenewTaskClaim(ctx context.Context, taskID string, request RenewClaimRequest) (ClaimResponse, error) {
	var response ClaimResponse
	path := fmt.Sprintf("/api/v1/collaboration/tasks/%s/claim/renew", url.PathEscape(strings.TrimSpace(taskID)))
	if err := c.doJSON(ctx, c.securityBaseURL, http.MethodPost, path, nil, request, &response); err != nil {
		return ClaimResponse{}, err
	}
	return response, nil
}

func (c *Client) StartTask(ctx context.Context, taskID string, request StartTaskRequest) (Task, error) {
	var response Task
	path := fmt.Sprintf("/api/v1/collaboration/tasks/%s/start", url.PathEscape(strings.TrimSpace(taskID)))
	if err := c.doJSON(ctx, c.securityBaseURL, http.MethodPost, path, nil, request, &response); err != nil {
		return Task{}, err
	}
	return response, nil
}

func (c *Client) CompleteTask(ctx context.Context, taskID string, request CompleteTaskRequest) (Task, error) {
	var response Task
	path := fmt.Sprintf("/api/v1/collaboration/tasks/%s/complete", url.PathEscape(strings.TrimSpace(taskID)))
	if err := c.doJSON(ctx, c.securityBaseURL, http.MethodPost, path, nil, request, &response); err != nil {
		return Task{}, err
	}
	return response, nil
}

func (c *Client) FailTask(ctx context.Context, taskID string, request FailTaskRequest) (Task, error) {
	var response Task
	path := fmt.Sprintf("/api/v1/collaboration/tasks/%s/fail", url.PathEscape(strings.TrimSpace(taskID)))
	if err := c.doJSON(ctx, c.securityBaseURL, http.MethodPost, path, nil, request, &response); err != nil {
		return Task{}, err
	}
	return response, nil
}

func (c *Client) UploadFile(ctx context.Context, request FileUploadRequest) (RawJSON, error) {
	bodyReader := request.ContentReader
	if bodyReader == nil {
		bodyReader = bytes.NewReader(request.Content)
	}
	body, writer := io.Pipe()
	form := multipart.NewWriter(writer)
	go func() {
		if err := writeFileUploadMultipart(form, request, bodyReader); err != nil {
			_ = writer.CloseWithError(err)
			return
		}
		if err := form.Close(); err != nil {
			_ = writer.CloseWithError(err)
			return
		}
		_ = writer.Close()
	}()

	headers := c.internalAuthHeaders()
	headers["Content-Type"] = form.FormDataContentType()

	path := "/api/v1/workspace-files/upload"
	if strings.TrimSpace(request.AssistantID) == "" && strings.TrimSpace(request.UserID) != "" {
		path = "/api/v1/workspace-files/push"
	}

	return c.doRawJSON(ctx, c.securityBaseURL, http.MethodPost, path, headers, body)
}

func writeFileUploadMultipart(writer *multipart.Writer, request FileUploadRequest, content io.Reader) error {
	if strings.TrimSpace(request.AssistantID) != "" {
		if err := writer.WriteField("assistant_id", strings.TrimSpace(request.AssistantID)); err != nil {
			return err
		}
	}
	if strings.TrimSpace(request.UserID) != "" {
		if err := writer.WriteField("user_id", strings.TrimSpace(request.UserID)); err != nil {
			return err
		}
	}
	targetBucket := strings.TrimSpace(request.TargetBucket)
	if targetBucket == "" {
		targetBucket = "temp"
	}
	if err := writer.WriteField("target_bucket", targetBucket); err != nil {
		return err
	}
	if request.Overwrite {
		if err := writer.WriteField("overwrite", "true"); err != nil {
			return err
		}
	}
	if strings.TrimSpace(request.SourceType) != "" {
		if err := writer.WriteField("source_type", strings.TrimSpace(request.SourceType)); err != nil {
			return err
		}
	}
	if strings.TrimSpace(request.SourceRef) != "" {
		if err := writer.WriteField("source_ref", strings.TrimSpace(request.SourceRef)); err != nil {
			return err
		}
	}
	if strings.TrimSpace(request.UploaderUserID) != "" {
		if err := writer.WriteField("uploader_user_id", strings.TrimSpace(request.UploaderUserID)); err != nil {
			return err
		}
	}

	part, err := createUploadFilePart(writer, strings.TrimSpace(request.Filename), strings.TrimSpace(request.ContentType))
	if err != nil {
		return err
	}
	if _, err := io.Copy(part, content); err != nil {
		return err
	}
	return nil
}

func createUploadFilePart(writer *multipart.Writer, filename string, contentType string) (io.Writer, error) {
	if contentType == "" {
		contentType = "application/octet-stream"
	}
	header := make(textproto.MIMEHeader)
	header.Set(
		"Content-Disposition",
		fmt.Sprintf(`form-data; name="file"; filename="%s"`, escapeMultipartValue(filename)),
	)
	header.Set("Content-Type", contentType)
	return writer.CreatePart(header)
}

func escapeMultipartValue(value string) string {
	return strings.NewReplacer("\\", "\\\\", `"`, "\\\"").Replace(value)
}

func (c *Client) ListFiles(ctx context.Context, request FileListRequest) (RawJSON, error) {
	query := url.Values{}
	if strings.TrimSpace(request.AssistantID) != "" {
		query.Set("assistant_id", strings.TrimSpace(request.AssistantID))
	}
	if strings.TrimSpace(request.UserID) != "" {
		query.Set("user_id", strings.TrimSpace(request.UserID))
	}
	if strings.TrimSpace(request.Bucket) != "" {
		query.Set("bucket", strings.TrimSpace(request.Bucket))
	}
	if strings.TrimSpace(request.Direction) != "" {
		query.Set("direction", strings.TrimSpace(request.Direction))
	}
	if strings.TrimSpace(request.FileRole) != "" {
		query.Set("file_role", strings.TrimSpace(request.FileRole))
	}
	path := "/api/v1/workspace-files"
	if encoded := query.Encode(); encoded != "" {
		path += "?" + encoded
	}
	headers := c.internalAuthHeaders()
	return c.doRawJSON(ctx, c.securityBaseURL, http.MethodGet, path, headers, nil)
}

func (c *Client) SyncResults(ctx context.Context, request SyncResultsRequest) (WorkspaceScanSummary, error) {
	var response WorkspaceScanSummary
	query := url.Values{}
	if strings.TrimSpace(request.AssistantID) != "" {
		query.Set("assistant_id", strings.TrimSpace(request.AssistantID))
	}
	if strings.TrimSpace(request.Bucket) != "" {
		query.Set("bucket", strings.TrimSpace(request.Bucket))
	}
	if strings.TrimSpace(request.UserID) != "" {
		query.Set("user_id", strings.TrimSpace(request.UserID))
	}
	if !request.ChangedAfter.IsZero() {
		query.Set("changed_after", request.ChangedAfter.UTC().Format(time.RFC3339Nano))
	}
	if strings.TrimSpace(request.ResultPathPrefix) != "" {
		query.Set("result_path_prefix", strings.TrimSpace(request.ResultPathPrefix))
	}
	if strings.TrimSpace(request.TaskID) != "" {
		query.Set("task_id", strings.TrimSpace(request.TaskID))
	}
	if strings.TrimSpace(request.SourceRef) != "" {
		query.Set("source_ref", strings.TrimSpace(request.SourceRef))
	}
	path := "/api/v1/workspace-files/sync-results"
	if encoded := query.Encode(); encoded != "" {
		path += "?" + encoded
	}
	headers := c.internalAuthHeaders()
	if err := c.doJSONTransientRetry(ctx, c.securityBaseURL, http.MethodPost, path, headers, nil, &response); err != nil {
		return WorkspaceScanSummary{}, err
	}
	return response, nil
}

func (c *Client) doJSONTransientRetry(
	ctx context.Context,
	baseURL string,
	method string,
	path string,
	headers map[string]string,
	requestBody any,
	responseBody any,
) error {
	delays := []time.Duration{100 * time.Millisecond, 250 * time.Millisecond, 500 * time.Millisecond, time.Second}
	var lastErr error
	for attempt := 0; attempt <= len(delays); attempt++ {
		if attempt > 0 && responseBody != nil {
			resetJSONResponseBody(responseBody)
		}
		err := c.doJSON(ctx, baseURL, method, path, headers, requestBody, responseBody)
		if err == nil {
			return nil
		}
		lastErr = err
		if !isTransientSouthboundError(err) || attempt == len(delays) {
			return err
		}
		timer := time.NewTimer(delays[attempt])
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-timer.C:
		}
	}
	return lastErr
}

func resetJSONResponseBody(responseBody any) {
	if responseBody == nil {
		return
	}
	value := reflect.ValueOf(responseBody)
	if value.Kind() != reflect.Ptr || value.IsNil() {
		return
	}
	elem := value.Elem()
	if !elem.CanSet() {
		return
	}
	elem.Set(reflect.Zero(elem.Type()))
}

func isTransientSouthboundError(err error) bool {
	var httpErr *HTTPError
	if errors.As(err, &httpErr) {
		switch httpErr.StatusCode {
		case http.StatusBadGateway, http.StatusServiceUnavailable, http.StatusGatewayTimeout:
			return true
		default:
			return httpErr.StatusCode >= 500
		}
	}
	return false
}

func (c *Client) GetFile(ctx context.Context, fileID string) (RawJSON, error) {
	path := fmt.Sprintf("/api/v1/workspace-files/%s", url.PathEscape(strings.TrimSpace(fileID)))
	headers := c.internalAuthHeaders()
	return c.doRawJSON(ctx, c.securityBaseURL, http.MethodGet, path, headers, nil)
}

func (c *Client) DownloadFile(
	ctx context.Context,
	fileID string,
	disposition string,
) (FileDownloadResponse, error) {
	stream, err := c.DownloadFileStream(ctx, fileID, disposition)
	if err != nil {
		return FileDownloadResponse{}, err
	}
	defer stream.ResponseBody.Close()
	payload, err := io.ReadAll(stream.ResponseBody)
	if err != nil {
		return FileDownloadResponse{}, err
	}
	return FileDownloadResponse{
		Content:            payload,
		ContentType:        stream.ContentType,
		ContentDisposition: stream.ContentDisposition,
		DownloadSource:     stream.DownloadSource,
		StorageBackend:     stream.StorageBackend,
	}, nil
}

func (c *Client) DownloadFileStream(
	ctx context.Context,
	fileID string,
	disposition string,
) (DownloadReader, error) {
	path := fmt.Sprintf("/api/v1/workspace-files/%s/download", url.PathEscape(strings.TrimSpace(fileID)))
	if trimmed := strings.TrimSpace(disposition); trimmed != "" {
		path += "?disposition=" + url.QueryEscape(trimmed)
	}
	startedAt := time.Now()
	statusCode := 0
	failed := true
	defer func() {
		metrics.RecordSouthbound(
			c.componentForBaseURL(c.securityBaseURL),
			http.MethodGet,
			normalizeMetricPath(path),
			statusCode,
			time.Since(startedAt),
			failed,
		)
	}()

	request, err := http.NewRequestWithContext(ctx, http.MethodGet, c.securityBaseURL+path, nil)
	if err != nil {
		return DownloadReader{}, fmt.Errorf("build GET %s request: %w", c.securityBaseURL+path, err)
	}
	request.Header.Set("Accept", "*/*")
	for key, value := range c.internalAuthHeaders() {
		request.Header.Set(key, value)
	}

	response, err := c.httpClient.Do(request)
	if err != nil {
		return DownloadReader{}, fmt.Errorf("GET %s failed: %w", c.securityBaseURL+path, err)
	}
	statusCode = response.StatusCode

	if response.StatusCode < 200 || response.StatusCode >= 300 {
		payload, _ := io.ReadAll(response.Body)
		_ = response.Body.Close()
		return DownloadReader{}, &HTTPError{
			StatusCode: response.StatusCode,
			Method:     http.MethodGet,
			URL:        c.securityBaseURL + path,
			Body:       strings.TrimSpace(string(payload)),
		}
	}
	failed = false

	return DownloadReader{
		ResponseBody:       response.Body,
		ContentType:        response.Header.Get("Content-Type"),
		ContentDisposition: response.Header.Get("Content-Disposition"),
		DownloadSource:     response.Header.Get("X-IronAgent-Download-Source"),
		StorageBackend:     response.Header.Get("X-IronAgent-Storage-Backend"),
	}, nil
}

func (c *Client) DeleteFile(ctx context.Context, fileID string, userID string) (RawJSON, error) {
	path := fmt.Sprintf("/api/v1/workspace-files/%s/delete", url.PathEscape(strings.TrimSpace(fileID)))
	if trimmed := strings.TrimSpace(userID); trimmed != "" {
		path += "?user_id=" + url.QueryEscape(trimmed)
	}
	headers := c.internalAuthHeaders()
	return c.doRawJSON(ctx, c.securityBaseURL, http.MethodPost, path, headers, nil)
}

func (c *Client) CheckHealth(ctx context.Context) (HealthSummary, error) {
	checkedAt := time.Now().UTC()
	return HealthSummary{
		Components: []HealthComponent{
			c.checkHealthEndpoint(ctx, "southbound_security", c.securityBaseURL),
			c.checkHealthEndpointPaths(
				ctx,
				"southbound_collaboration",
				c.securityBaseURL,
				[]string{"/api/v1/collaboration/readyz"},
			),
			c.checkHealthEndpoint(ctx, "southbound_admin", c.adminBaseURL),
		},
		CheckedAt: checkedAt,
	}, nil
}

func (c *Client) checkHealthEndpoint(ctx context.Context, name string, baseURL string) HealthComponent {
	return c.checkHealthEndpointPaths(
		ctx,
		name,
		baseURL,
		[]string{"/healthz", "/api/v1/health", "/health"},
	)
}

func (c *Client) checkHealthEndpointPaths(ctx context.Context, name string, baseURL string, paths []string) HealthComponent {
	component := HealthComponent{Name: name, Status: "down"}
	baseURL = strings.TrimRight(strings.TrimSpace(baseURL), "/")
	if baseURL == "" {
		component.Error = "base url is empty"
		return component
	}
	for _, path := range paths {
		statusCode, err := c.probeHealthPath(ctx, baseURL+path)
		if err == nil && statusCode >= 200 && statusCode < 300 {
			component.Status = "up"
			component.StatusCode = statusCode
			component.Error = ""
			return component
		}
		component.StatusCode = statusCode
		if err != nil {
			component.Error = err.Error()
			continue
		}
		component.Error = fmt.Sprintf("status %d", statusCode)
		if statusCode != http.StatusNotFound {
			return component
		}
	}
	return component
}

func (c *Client) probeHealthPath(ctx context.Context, fullURL string) (int, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, fullURL, nil)
	if err != nil {
		return 0, fmt.Errorf("build GET %s request: %w", fullURL, err)
	}
	request.Header.Set("Accept", "application/json")
	for key, value := range c.internalAuthHeaders() {
		request.Header.Set(key, value)
	}
	response, err := c.httpClient.Do(request)
	if err != nil {
		return 0, fmt.Errorf("GET %s failed: %w", fullURL, err)
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, response.Body)
	return response.StatusCode, nil
}

func (c *Client) doJSON(
	ctx context.Context,
	baseURL string,
	method string,
	path string,
	headers map[string]string,
	requestBody any,
	responseBody any,
) error {
	fullURL := baseURL + path
	startedAt := time.Now()
	statusCode := 0
	failed := true
	defer func() {
		metrics.RecordSouthbound(
			c.componentForBaseURL(baseURL),
			method,
			normalizeMetricPath(path),
			statusCode,
			time.Since(startedAt),
			failed,
		)
	}()

	var body io.Reader
	if requestBody != nil {
		payload, err := json.Marshal(requestBody)
		if err != nil {
			return fmt.Errorf("marshal %s %s request: %w", method, fullURL, err)
		}
		body = bytes.NewReader(payload)
	}

	request, err := http.NewRequestWithContext(ctx, method, fullURL, body)
	if err != nil {
		return fmt.Errorf("build %s %s request: %w", method, fullURL, err)
	}
	request.Header.Set("Accept", "application/json")
	if requestBody != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	for key, value := range headers {
		request.Header.Set(key, value)
	}

	response, err := c.httpClient.Do(request)
	if err != nil {
		return fmt.Errorf("%s %s failed: %w", method, fullURL, err)
	}
	defer response.Body.Close()
	statusCode = response.StatusCode

	payload, err := io.ReadAll(response.Body)
	if err != nil {
		return fmt.Errorf("read %s %s response: %w", method, fullURL, err)
	}

	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return &HTTPError{
			StatusCode: response.StatusCode,
			Method:     method,
			URL:        fullURL,
			Body:       strings.TrimSpace(string(payload)),
		}
	}

	if responseBody == nil || len(payload) == 0 {
		failed = false
		return nil
	}

	if err := json.Unmarshal(payload, responseBody); err != nil {
		return fmt.Errorf("decode %s %s response: %w", method, fullURL, err)
	}
	failed = false
	return nil
}

func isHTTPStatus(err error, statusCode int) bool {
	httpErr, ok := err.(*HTTPError)
	return ok && httpErr.StatusCode == statusCode
}

func (c *Client) doRawJSON(
	ctx context.Context,
	baseURL string,
	method string,
	path string,
	headers map[string]string,
	body io.Reader,
) (RawJSON, error) {
	fullURL := baseURL + path
	startedAt := time.Now()
	statusCode := 0
	failed := true
	defer func() {
		metrics.RecordSouthbound(
			c.componentForBaseURL(baseURL),
			method,
			normalizeMetricPath(path),
			statusCode,
			time.Since(startedAt),
			failed,
		)
	}()
	request, err := http.NewRequestWithContext(ctx, method, fullURL, body)
	if err != nil {
		return nil, fmt.Errorf("build %s %s request: %w", method, fullURL, err)
	}
	request.Header.Set("Accept", "application/json")
	for key, value := range headers {
		request.Header.Set(key, value)
	}

	response, err := c.httpClient.Do(request)
	if err != nil {
		return nil, fmt.Errorf("%s %s failed: %w", method, fullURL, err)
	}
	defer response.Body.Close()
	statusCode = response.StatusCode

	payload, err := io.ReadAll(response.Body)
	if err != nil {
		return nil, fmt.Errorf("read %s %s response: %w", method, fullURL, err)
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, &HTTPError{
			StatusCode: response.StatusCode,
			Method:     method,
			URL:        fullURL,
			Body:       strings.TrimSpace(string(payload)),
		}
	}

	var result RawJSON
	if err := json.Unmarshal(payload, &result); err != nil {
		return nil, fmt.Errorf("decode %s %s response: %w", method, fullURL, err)
	}
	failed = false
	return result, nil
}

func (c *Client) componentForBaseURL(baseURL string) string {
	switch strings.TrimRight(strings.TrimSpace(baseURL), "/") {
	case c.adminBaseURL:
		return "admin"
	case c.securityBaseURL:
		return "security"
	default:
		return "unknown"
	}
}

func normalizeMetricPath(path string) string {
	rawPath, _, _ := strings.Cut(strings.TrimSpace(path), "?")
	if rawPath == "" {
		return "unknown"
	}
	parts := strings.Split(rawPath, "/")
	for i := range parts {
		if i == 0 {
			continue
		}
		previous := parts[i-1]
		switch previous {
		case "users", "tasks", "approvals", "assistants", "workspace-files":
			parts[i] = "{id}"
		}
	}
	return strings.Join(parts, "/")
}
