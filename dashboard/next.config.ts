import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // postgres.js opens real TCP sockets, so every route that touches the
  // database runs on the Node runtime, never the edge one. Declared per-route
  // rather than globally; this is here for the shared bits.
  serverExternalPackages: ["postgres"],
};

export default nextConfig;
