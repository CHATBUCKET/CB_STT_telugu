# Stage 1: install the Python packages (prebuilt wheels only) into /deps.
FROM python:3.13-slim-trixie AS build
COPY requirements.txt .
RUN pip install --no-cache-dir --only-binary=:all: --target /deps -r requirements.txt \
 && find /deps -depth -type d -name tests -exec rm -rf {} +

# Stage 2: distroless Python 3.13 - no shell, no package manager, no pip,
# runs as the non-root user 65532. The model is baked into the image.
FROM gcr.io/distroless/python3-debian13:nonroot
ARG S2T_LANGUAGE=telugu
ENV PYTHONPATH=/deps \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    S2T_LANGUAGE=${S2T_LANGUAGE} \
    S2T_MODEL_DIR=/models \
    S2T_PORT=6008
COPY --from=build /deps /deps
COPY models/${S2T_LANGUAGE} /models/${S2T_LANGUAGE}
WORKDIR /srv
COPY app ./app
EXPOSE 6008
ENTRYPOINT ["python3", "-m", "app"]
