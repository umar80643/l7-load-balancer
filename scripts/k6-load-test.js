// k6 load test for the L7 load balancer.
//
// Install k6: https://k6.io/docs/get-started/installation/
// Run:        k6 run scripts/k6-load-test.js
// Custom target: k6 run -e BASE_URL=http://your-lb:8080 scripts/k6-load-test.js
//
// This script ramps concurrent virtual users up and back down, exercising
// a mix of paths, and asserts on both correctness (status 200) and latency
// (p95 under a threshold) -- a load test that only measures throughput
// without also asserting on correctness/latency isn't actually verifying
// the system behaves well under load, just that it responds *somehow*.
import http from "k6/http";
import { check, sleep } from "k6";
import { Rate, Trend } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8080";

const errorRate = new Rate("errors");
const backendLatency = new Trend("backend_latency", true);

export const options = {
  stages: [
    { duration: "10s", target: 20 },   // ramp up to 20 VUs
    { duration: "30s", target: 20 },   // hold at 20 VUs
    { duration: "10s", target: 100 },  // spike to 100 VUs
    { duration: "30s", target: 100 },  // hold at 100 VUs
    { duration: "10s", target: 0 },    // ramp down
  ],
  thresholds: {
    http_req_duration: ["p(95)<500", "p(99)<1000"],
    errors: ["rate<0.01"], // fewer than 1% errors
  },
};

const paths = ["/", "/api/widgets", "/api/users/42", "/health"];

export default function () {
  const path = paths[Math.floor(Math.random() * paths.length)];
  const res = http.get(`${BASE_URL}${path}`);

  const ok = check(res, {
    "status is 200": (r) => r.status === 200,
    "has body": (r) => r.body && r.body.length > 0,
  });

  errorRate.add(!ok);
  backendLatency.add(res.timings.duration);

  sleep(Math.random() * 0.5); // simulate think time between requests
}
