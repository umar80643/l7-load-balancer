// A companion to k6-load-test.js focused purely on peak throughput: no
// think-time between requests, so virtual users hammer the load balancer
// as fast as they can. Useful for finding the practical req/s ceiling,
// as opposed to k6-load-test.js which simulates more realistic client
// behavior (varied paths, pauses between requests).
import http from "k6/http";
import { check } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8080";

export const options = {
  scenarios: {
    throughput: {
      executor: "constant-vus",
      vus: Number(__ENV.VUS || 50),
      duration: __ENV.DURATION || "15s",
    },
  },
};

export default function () {
  const res = http.get(`${BASE_URL}/`);
  check(res, { "status is 200": (r) => r.status === 200 });
}
