package rl

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestStatusRequiresTokenWhenConfigured(t *testing.T) {
	t.Setenv("RL_API_TOKEN", "secret-token")
	server := NewWebhookServer(NewKnowledgeBase(), NewGAPopulation(2), NewDQNAgent(6, 4))
	req := httptest.NewRequest(http.MethodGet, "/rl/status", nil)
	rr := httptest.NewRecorder()

	server.HandleStatus(rr, req)

	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 without token, got %d", rr.Code)
	}
}

func TestTVAlertRequiresTokenWhenConfigured(t *testing.T) {
	t.Setenv("RL_API_TOKEN", "secret-token")
	server := NewWebhookServer(NewKnowledgeBase(), NewGAPopulation(2), NewDQNAgent(6, 4))
	req := httptest.NewRequest(http.MethodPost, "/tv/alert", nil)
	rr := httptest.NewRecorder()

	server.HandleTVAlert(rr, req)

	if rr.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 without token, got %d", rr.Code)
	}
}

func TestStatusAcceptsBearerToken(t *testing.T) {
	t.Setenv("RL_API_TOKEN", "secret-token")
	server := NewWebhookServer(NewKnowledgeBase(), NewGAPopulation(2), NewDQNAgent(6, 4))
	req := httptest.NewRequest(http.MethodGet, "/rl/status", nil)
	req.Header.Set("Authorization", "Bearer secret-token")
	rr := httptest.NewRecorder()

	server.HandleStatus(rr, req)

	if rr.Code != http.StatusOK {
		t.Fatalf("expected 200 with token, got %d", rr.Code)
	}
}
