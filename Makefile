# Lumiverb — unified build commands
#
# Usage:
#   make help          Show all targets
#   make test          Run all tests (Python + Swift)
#   make build-macos   Build macOS native app
#   make run-api       Start API dev server

.PHONY: help test test-python test-swift test-fast test-slow \
        build-macos build-ios build-swift-lib \
        run-api run-web run-macos \
        generate-xcode clean clean-swift clean-python \
        lint check

SHELL := /bin/bash

# Paths
SWIFT_PROJECT := clients/lumiverb-app
SWIFT_PACKAGE := $(SWIFT_PROJECT)/Sources/LumiverbKit
XCODE_PROJECT := $(SWIFT_PROJECT)/Lumiverb.xcodeproj
WEB_DIR       := src/ui/web

# Tools
UV       := uv
SWIFT    := swift
XCODEGEN := xcodegen
XBUILD   := xcodebuild

# iOS simulator (override with: make build-ios IOS_DEST="platform=iOS Simulator,name=iPhone 15")
IOS_DEST ?= platform=iOS Simulator,name=iPhone 16

# ──────────────────────────────────────────────
# Help
# ──────────────────────────────────────────────

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ──────────────────────────────────────────────
# Test
# ──────────────────────────────────────────────

test: test-python test-swift ## Run all tests

test-python: ## Run Python test suite (fast + slow)
	$(UV) run pytest tests/ -q

test-fast: ## Run fast Python tests only (no DB)
	$(UV) run pytest tests/ -m fast -q

test-slow: ## Run slow Python tests only (requires DB)
	$(UV) run pytest tests/ -m slow -q

test-swift: build-swift-lib ## Run Swift package tests
	cd $(SWIFT_PACKAGE) && $(SWIFT) test

test-swift-xcode: generate-xcode ## Run Swift tests via Xcode (macOS target)
	cd $(SWIFT_PROJECT) && $(XBUILD) test \
		-project Lumiverb.xcodeproj \
		-scheme LumiverbKitTests \
		-configuration Debug \
		-quiet

# ──────────────────────────────────────────────
# Build
# ──────────────────────────────────────────────

build-swift-lib: ## Build shared Swift package (no Xcode required)
	cd $(SWIFT_PACKAGE) && $(SWIFT) build

build-macos: generate-xcode ## Build macOS app
	cd $(SWIFT_PROJECT) && $(XBUILD) build \
		-project Lumiverb.xcodeproj \
		-scheme Lumiverb-macOS \
		-configuration Debug \
		-quiet

build-ios: generate-xcode ## Build iOS app (simulator)
	cd $(SWIFT_PROJECT) && $(XBUILD) build \
		-project Lumiverb.xcodeproj \
		-scheme Lumiverb-iOS \
		-configuration Debug \
		-destination '$(IOS_DEST)' \
		-quiet

build-web: ## Build web UI (TypeScript + Vite)
	cd $(WEB_DIR) && npx vite build

generate-xcode: ## Generate Xcode project from project.yml
	cd $(SWIFT_PROJECT) && $(XCODEGEN) generate

# ──────────────────────────────────────────────
# Run
# ──────────────────────────────────────────────

run-api: ## Start API dev server (port 8000)
	$(UV) run uvicorn src.server.api.main:app --reload --port 8000

run-web: ## Start web UI dev server (port 5173)
	cd $(WEB_DIR) && npx vite

run-macos: build-macos ## Build and launch macOS app
	@app=$$(find $(SWIFT_PROJECT)/build -name "Lumiverb.app" -path "*/Debug/*" 2>/dev/null | head -1); \
	if [ -z "$$app" ]; then \
		echo "Build succeeded but .app not found in expected location."; \
		echo "Open $(XCODE_PROJECT) in Xcode and run from there."; \
	else \
		open "$$app"; \
	fi

# ──────────────────────────────────────────────
# Lint / Check
# ──────────────────────────────────────────────

lint: ## Run Python linter (ruff)
	$(UV) run ruff check src/ tests/

check: lint test-fast ## Quick pre-commit check (lint + fast tests)
	cd $(WEB_DIR) && npx tsc --noEmit

# ──────────────────────────────────────────────
# Clean
# ──────────────────────────────────────────────

clean: clean-python clean-swift ## Remove all build artifacts

clean-python: ## Remove Python caches
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true

clean-swift: ## Remove Swift/Xcode build artifacts
	cd $(SWIFT_PACKAGE) && $(SWIFT) package clean 2>/dev/null || true
	rm -rf $(SWIFT_PROJECT)/build
	rm -rf $(SWIFT_PROJECT)/DerivedData
	rm -rf $(XCODE_PROJECT)
