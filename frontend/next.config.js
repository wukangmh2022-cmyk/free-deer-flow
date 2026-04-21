/**
 * Run `build` or `dev` with `SKIP_ENV_VALIDATION` to skip env validation. This is especially useful
 * for Docker builds.
 */
import "./src/env.js";

function getInternalServiceURL(envKey, fallbackURL) {
  const configured = process.env[envKey]?.trim();
  return configured && configured.length > 0
    ? configured.replace(/\/+$/, "")
    : fallbackURL;
}
import nextra from "nextra";

const withNextra = nextra({});
const isDesktopStaticBuild = process.env.DEER_FLOW_DESKTOP_STATIC === "1";

/** @type {import("next").NextConfig} */
const config = {
  output: isDesktopStaticBuild ? "export" : "standalone",
  i18n: {
    locales: ["en", "zh"],
    defaultLocale: "en",
  },
  typescript: {
    ignoreBuildErrors: process.env.DEER_FLOW_IGNORE_BUILD_ERRORS === "1",
  },
  devIndicators: false,
  async rewrites() {
    const rewrites = [];
    const langgraphURL = getInternalServiceURL(
      "DEER_FLOW_INTERNAL_LANGGRAPH_BASE_URL",
      "http://127.0.0.1:2024",
    );
    const gatewayURL = getInternalServiceURL(
      "DEER_FLOW_INTERNAL_GATEWAY_BASE_URL",
      "http://127.0.0.1:8001",
    );

    if (!isDesktopStaticBuild && !process.env.NEXT_PUBLIC_LANGGRAPH_BASE_URL) {
      rewrites.push({
        source: "/api/langgraph",
        destination: langgraphURL,
      });
      rewrites.push({
        source: "/api/langgraph/:path*",
        destination: `${langgraphURL}/:path*`,
      });
      rewrites.push({
        source: "/api/langgraph-compat",
        destination: `${gatewayURL}/api/langgraph-compat`,
      });
      rewrites.push({
        source: "/api/langgraph-compat/:path*",
        destination: `${gatewayURL}/api/langgraph-compat/:path*`,
      });
    }

    if (!isDesktopStaticBuild && !process.env.NEXT_PUBLIC_BACKEND_BASE_URL) {
      rewrites.push({
        source: "/api/agents",
        destination: `${gatewayURL}/api/agents`,
      });
      rewrites.push({
        source: "/api/agents/:path*",
        destination: `${gatewayURL}/api/agents/:path*`,
      });
      rewrites.push({
        source: "/api/provider-auth",
        destination: `${gatewayURL}/api/provider-auth`,
      });
      rewrites.push({
        source: "/api/provider-auth/:path*",
        destination: `${gatewayURL}/api/provider-auth/:path*`,
      });
    }

    return rewrites;
  },
};

export default withNextra(config);
