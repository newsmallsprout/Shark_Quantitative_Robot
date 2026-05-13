package main

import "testing"

func TestRedisOptionsFromURLUsesConfiguredHostPasswordAndDB(t *testing.T) {
	opt, err := redisOptionsFromURL("redis://:pass@example.test:6380/3")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if opt.Addr != "example.test:6380" {
		t.Fatalf("expected configured address, got %q", opt.Addr)
	}
	if opt.Password != "pass" {
		t.Fatalf("expected password from URL, got %q", opt.Password)
	}
	if opt.DB != 3 {
		t.Fatalf("expected DB 3, got %d", opt.DB)
	}
}
