import type { NextConfig } from "next";

const AGENT_ORIGIN = process.env.AGENT_PROXY_TARGET ?? "http://127.0.0.1:8080";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${AGENT_ORIGIN}/api/:path*` },
      { source: "/ws/:path*", destination: `${AGENT_ORIGIN}/ws/:path*` },
      { source: "/health", destination: `${AGENT_ORIGIN}/health` },
    ];
  },
};

export default nextConfig;
