.PHONY: setup start stop test logs clean ollama-setup

# ── Setup ─────────────────────────────────────────────
setup:
	docker-compose up -d postgres
	@echo "Waiting for Postgres..."
	@sleep 3
	cd bobclaw-core && pip install -r requirements.txt
	cd bobclaw-gateway && pip install -r requirements.txt
	cd bobclaw-claude-pipeline && pip install -r requirements.txt
	@echo "✅ Setup complete"

setup-full:
	docker-compose --profile full up -d
	cd bobclaw-core && pip install -r requirements.txt
	cd bobclaw-gateway && pip install -r requirements.txt
	cd bobclaw-claude-pipeline && pip install -r requirements.txt

# ── Start/Stop ────────────────────────────────────────
start:
	@echo "Starting BoBClaw services..."
	cd bobclaw-core && python start.py &
	cd bobclaw-gateway && PYTHONPATH=../bobclaw-core python gateway.py &
	cd bobclaw-claude-pipeline && python pipeline.py &
	@echo "✅ All services starting"
	@echo "  Core:     http://localhost:7825"
	@echo "  Gateway:  http://localhost:7826"
	@echo "  Pipeline: http://localhost:7823"

stop:
	@pkill -f "python start.py" || true
	@pkill -f "python gateway.py" || true
	@pkill -f "python pipeline.py" || true
	@echo "✅ Services stopped"

# ── Testing ───────────────────────────────────────────
test:
	cd bobclaw-core && pytest tests/ -v
	cd bobclaw-gateway && pytest tests/ -v
	cd bobclaw-claude-pipeline && pytest tests/ -v
	@echo "✅ All tests passed"

test-core:
	cd bobclaw-core && pytest tests/ -v

test-gateway:
	cd bobclaw-gateway && pytest tests/ -v

test-pipeline:
	cd bobclaw-claude-pipeline && pytest tests/ -v

# ── Logging ───────────────────────────────────────────
logs:
	@echo "=== Docker Logs ==="
	docker-compose logs --tail=50
	@echo ""

# ── Cleanup ───────────────────────────────────────────
clean:
	docker-compose down -v
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	find . -name "bobclaw_cache.db" -delete 2>/dev/null || true
	@echo "✅ Cleaned"

# ── Ollama Setup ──────────────────────────────────────
ollama-setup:
	curl -fsSL https://ollama.com/install.sh | sh
	ollama pull gemma4:27b
	sudo systemctl enable ollama
	sudo systemctl start ollama
	@echo "✅ Ollama ready with Gemma 4 27B"

# ── Status ────────────────────────────────────────────
status:
	@echo "=== Docker ==="
	@docker-compose ps
	@echo ""
	@echo "=== Ollama ==="
	@curl -s http://localhost:11434/v1/models 2>/dev/null | python3 -m json.tool || echo "Ollama: not running"
	@echo ""
	@echo "=== Services ==="
	@curl -s http://localhost:7825/health 2>/dev/null || echo "Core (7825): not running"
	@curl -s http://localhost:7826/health 2>/dev/null || echo "Gateway (7826): not running"
	@curl -s http://localhost:7823/health 2>/dev/null || echo "Pipeline (7823): not running"
