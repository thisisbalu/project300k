# syntax=docker/dockerfile:1

# --- build stage -------------------------------------------------------------
# Compiles a fully static binary (CGO disabled; pgx is pure Go). The migrations
# are embedded into the binary via go:embed, so the runtime image needs nothing
# but the binary itself.
FROM golang:1.26-alpine AS build
WORKDIR /src

# Cache module downloads separately from source so code changes don't re-pull deps.
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-s -w" -o /out/server ./cmd/server

# --- runtime stage -----------------------------------------------------------
# Distroless "static" has no shell, package manager, or libc — minimal attack
# surface. The :nonroot tag runs as an unprivileged user by default.
FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/server /server
EXPOSE 8080
USER nonroot:nonroot
ENTRYPOINT ["/server"]
