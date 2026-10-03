IMAGE   := s2t-telugu
PORT    := 6008

.PHONY: build run stop logs health test clean

## Build the Docker image (multi-stage)
build:
	docker build -t $(IMAGE) .

## Run the container the way Cloud Run does (model is baked into the image)
run:
	docker run -d \
		--name $(IMAGE) \
		--read-only --cap-drop=ALL --security-opt no-new-privileges \
		-p $(PORT):$(PORT) \
		$(IMAGE)
	@echo "Started → http://localhost:$(PORT)"

## Stop and remove the container
stop:
	docker stop $(IMAGE) && docker rm $(IMAGE)

## Follow container logs
logs:
	docker logs -f $(IMAGE)

## Check health endpoint
health:
	curl -sf http://localhost:$(PORT)/health | python3 -m json.tool

## Quick smoke-test: build → run → health → stop
test: build run
	@sleep 5
	$(MAKE) health
	$(MAKE) stop

## Remove the image
clean:
	docker rmi $(IMAGE) 2>/dev/null || true

## Run smoke test locally (no Docker needed)
test-local:
	python3 tests/test_service.py
