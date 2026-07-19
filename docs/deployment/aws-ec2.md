# Deploying to AWS EC2

This guide covers running the full stack (load balancer + backend fleet +
Prometheus + Grafana, via Docker Compose) on a single EC2 instance. It's
intentionally the simplest viable production-ish deployment path -- a
single-instance Docker Compose deployment -- appropriate for a demo/resume
project or a small real workload. It is **not** a highly-available,
multi-AZ setup; see "Beyond a single instance" at the end for what that
would add.

## 1. Launch the instance

- **AMI**: Amazon Linux 2023 (or Ubuntu 22.04/24.04 -- adjust package
  manager commands below accordingly).
- **Instance type**: `t3.medium` (2 vCPU / 4 GiB) is enough to run the LB,
  3 backends, Prometheus, and Grafana comfortably for demo/light-load
  purposes. Size up if you're running the k6 load tests against it from
  the same box (the load generator competes for CPU with the LB).
- **Storage**: default 20-30 GiB gp3 is plenty; Prometheus/Grafana data
  volumes are small at this scale.
- **Security group**: open the following inbound ports (see "Security
  hardening" below before doing this in anything but a throwaway/demo
  account):
  - `22` (SSH) -- restrict to your IP, not `0.0.0.0/0`.
  - `8080` (load balancer) -- the public-facing port.
  - `3000` (Grafana) -- restrict to your IP or a VPN CIDR; don't expose
    Grafana with default credentials to the whole internet.
  - `9090` (Prometheus) -- same caution as Grafana; consider not exposing
    this publicly at all and only reaching it via SSH tunnel.
  - Do **NOT** open the admin API port publicly beyond what's needed --
    see the Security section below.

## 2. Install Docker and Docker Compose

```bash
# Amazon Linux 2023
sudo dnf update -y
sudo dnf install -y docker git
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user
# log out and back in for the group change to take effect

# Docker Compose v2 ships as a docker plugin on recent Docker versions;
# verify it's available:
docker compose version
```

```bash
# Ubuntu 22.04/24.04
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg git
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo $VERSION_CODENAME) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker ubuntu
```

## 3. Deploy the stack

```bash
git clone <your-fork-url> l7-load-balancer
cd l7-load-balancer
docker compose up --build -d
docker compose ps       # confirm everything is "Up"
curl http://localhost:8080/   # should get a response from one of the backends
```

Grafana is at `http://<instance-public-ip>:3000` (default admin/admin --
**change this immediately**, see below). Prometheus is at `:9090`.

## 4. Run as a systemd service (auto-restart on reboot/crash)

Docker Compose itself doesn't need a systemd unit if you set
`restart: unless-stopped` on every service (already done in
`docker-compose.yml`) plus enable the Docker daemon at boot
(`systemctl enable docker`, done above) -- Docker will restart the
containers automatically. If you want an explicit systemd unit anyway
(e.g. to tie stack lifecycle to a single `systemctl start/stop` command):

```ini
# /etc/systemd/system/l7lb.service
[Unit]
Description=L7 Load Balancer stack
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/home/ec2-user/l7-load-balancer
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now l7lb.service
```

## 5. Security hardening (read before exposing this beyond a demo)

- **Admin API**: `lb.admin` (mounted at `/admin/*`) is unauthenticated by
  design (see the docstring in `src/lb/admin/admin.py`) -- it lets any
  caller who can reach it add/remove backends, i.e. redirect production
  traffic. In this Compose deployment it's reachable on the same port
  (`8080`) as the public proxy traffic. For anything beyond a demo:
  put the admin routes behind a separate internal-only listener, a
  reverse-proxy path restriction (e.g. only allow `/admin/*` from the VPC
  CIDR), or add real authentication before exposing port 8080 publicly.
- **Grafana**: change `GF_SECURITY_ADMIN_PASSWORD` in `docker-compose.yml`
  away from `admin`, and set `GF_AUTH_ANONYMOUS_ENABLED=false` unless you
  specifically want anonymous viewer access.
- **TLS**: this project's load balancer speaks plain HTTP. For a public
  deployment, terminate TLS in front of it -- either an AWS Application
  Load Balancer / ALB in front of the EC2 instance (recommended: also
  gives you multi-AZ target groups for free), or a sidecar like Caddy/
  nginx doing TLS termination and proxying to `localhost:8080`.
- **Security group**: as noted above, don't open `9090`/`3000` to
  `0.0.0.0/0`; prefer an SSH tunnel or a VPN.

## 6. Running the benchmark against the deployed instance

From your local machine (with `k6` installed):

```bash
k6 run -e BASE_URL=http://<instance-public-ip>:8080 scripts/k6-load-test.js
```

Running the load generator *from a different machine* than the instance
under test (rather than from the EC2 box itself) avoids the load
generator's own CPU usage skewing the load balancer's measured latency --
this is the same reason cloud load-testing services run from separate
fleet.

## Beyond a single instance

This guide deliberately stops at "one EC2 instance running Docker
Compose" -- appropriate for the scope of this project. A genuinely
highly-available production setup would additionally need:

- Multiple EC2 instances (or ECS/EKS tasks) running the load balancer
  itself behind an AWS Network Load Balancer (L4) or Route 53 weighted
  DNS, since a single load balancer instance is itself a single point of
  failure.
- Backends spread across multiple Availability Zones.
- Centralized log aggregation (CloudWatch Logs, or shipping the JSON logs
  to an ELK/Loki stack) instead of `docker compose logs` on one box.
- A managed Prometheus (Amazon Managed Service for Prometheus) or a
  Prometheus HA pair, rather than a single Prometheus container whose data
  volume lives on one instance's EBS.
- Secrets (Grafana admin password, any future auth tokens) in AWS Secrets
  Manager / Parameter Store rather than plaintext in `docker-compose.yml`.

These are called out as concrete, scoped future-work items rather than
implemented here, since they're primarily infrastructure/ops concerns
orthogonal to the load balancer's own code.
