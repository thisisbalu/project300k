# syntax=docker/dockerfile:1

# --- build stage -------------------------------------------------------------
# Generates the templ Go from .templ files, then compiles a static binary.
# Assets (htmx, css, manifest) are go:embed'd, so the runtime needs only the binary.
FROM golang:1.26-alpine AS build
WORKDIR /src

COPY go.mod go.sum ./
RUN go mod download

COPY . .
# Regenerate templ code so the image never depends on committed *_templ.go.
RUN go run github.com/a-h/templ/cmd/templ@v0.3.1020 generate
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /out/web ./cmd/web

# --- runtime stage -----------------------------------------------------------
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/web /web
EXPOSE 8090
USER nonroot:nonroot
ENTRYPOINT ["/web"]
