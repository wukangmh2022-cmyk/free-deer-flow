const http = require("http");
const https = require("https");
const fs = require("fs");
const path = require("path");

const host = process.env.HOSTNAME || "127.0.0.1";
const port = Number(process.env.PORT || "3000");
const rootDir = process.env.DEER_FLOW_STATIC_ROOT || path.join(__dirname, "runtime", "frontend-static");
const gatewayBaseUrl = new URL(
  process.env.DEER_FLOW_INTERNAL_GATEWAY_BASE_URL || "http://127.0.0.1:8001",
);

const MIME_TYPES = {
  ".css": "text/css; charset=utf-8",
  ".gif": "image/gif",
  ".html": "text/html; charset=utf-8",
  ".ico": "image/x-icon",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".js": "application/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".map": "application/json; charset=utf-8",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".txt": "text/plain; charset=utf-8",
  ".webp": "image/webp",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

function sendFile(res, filePath) {
  const ext = path.extname(filePath).toLowerCase();
  res.writeHead(200, {
    "Content-Type": MIME_TYPES[ext] || "application/octet-stream",
    "Cache-Control": filePath.includes(`${path.sep}_next${path.sep}`)
      ? "public, max-age=31536000, immutable"
      : "no-cache",
  });
  fs.createReadStream(filePath).pipe(res);
}

function proxyRequest(req, res) {
  const upstreamTransport = gatewayBaseUrl.protocol === "https:" ? https : http;
  let requestPath = req.url || "/";
  const compatPrefixRewrites = [
    ["/api/langgraph-compat/assistants", "/api/assistants"],
    ["/api/langgraph-compat/threads", "/api/threads"],
    ["/api/langgraph-compat/runs", "/api/runs"],
  ];

  for (const [compatPrefix, upstreamPrefix] of compatPrefixRewrites) {
    if (
      requestPath === compatPrefix ||
      requestPath.startsWith(`${compatPrefix}/`) ||
      requestPath.startsWith(`${compatPrefix}?`)
    ) {
      requestPath = `${upstreamPrefix}${requestPath.slice(compatPrefix.length)}`;
      break;
    }
  }
  const proxyReq = upstreamTransport.request(
    {
      protocol: gatewayBaseUrl.protocol,
      hostname: gatewayBaseUrl.hostname,
      port: gatewayBaseUrl.port,
      method: req.method,
      path: requestPath,
      headers: {
        ...req.headers,
        host: gatewayBaseUrl.host,
      },
    },
    (proxyRes) => {
      res.writeHead(proxyRes.statusCode || 502, proxyRes.headers);
      proxyRes.pipe(res);
    },
  );

  proxyReq.on("error", (error) => {
    res.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
    res.end(`Bad Gateway: ${error.message}`);
  });

  req.pipe(proxyReq);
}

function tryResolveFile(requestPath) {
  const normalizedPath = decodeURIComponent(requestPath.split("?")[0]);
  const candidates = [];

  const safePath = normalizedPath.replace(/^\/+/, "");
  if (safePath.length === 0) {
    candidates.push("index.html");
  } else {
    candidates.push(safePath);
    candidates.push(path.join(safePath, "index.html"));
    candidates.push(`${safePath}.html`);
  }

  if (
    normalizedPath === "/workspace" ||
    normalizedPath === "/workspace/" ||
    normalizedPath.startsWith("/workspace/chats/")
  ) {
    candidates.push(path.join("workspace", "chats", "__desktop__.html"));
    candidates.push(path.join("workspace", "chats", "__desktop__", "index.html"));
  }

  if (normalizedPath.startsWith("/workspace/agents/")) {
    candidates.push(
      path.join(
        "workspace",
        "agents",
        "__desktop_agent__",
        "chats",
        "__desktop_thread__.html",
      ),
    );
    candidates.push(
      path.join(
        "workspace",
        "agents",
        "__desktop_agent__",
        "chats",
        "__desktop_thread__",
        "index.html",
      ),
    );
  }

  for (const candidate of candidates) {
    const resolved = path.resolve(rootDir, candidate);
    if (!resolved.startsWith(path.resolve(rootDir))) {
      continue;
    }
    if (fs.existsSync(resolved) && fs.statSync(resolved).isFile()) {
      return resolved;
    }
  }

  return null;
}

const server = http.createServer((req, res) => {
  const method = req.method || "GET";
  const requestPath = req.url || "/";

  if (
    requestPath.startsWith("/api/") ||
    requestPath === "/api" ||
    requestPath.startsWith("/v1/")
  ) {
    proxyRequest(req, res);
    return;
  }

  if (!["GET", "HEAD"].includes(method)) {
    res.writeHead(405, { "Content-Type": "text/plain; charset=utf-8" });
    res.end("Method Not Allowed");
    return;
  }

  const filePath = tryResolveFile(requestPath);
  if (!filePath) {
    res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
    res.end("Not Found");
    return;
  }

  if (method === "HEAD") {
    res.writeHead(200, {
      "Content-Type": MIME_TYPES[path.extname(filePath).toLowerCase()] || "application/octet-stream",
    });
    res.end();
    return;
  }

  sendFile(res, filePath);
});

server.listen(port, host, () => {
  process.stdout.write(`static-server listening on http://${host}:${port}\n`);
});
