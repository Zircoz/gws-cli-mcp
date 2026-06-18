import express from 'express';
import { spawn } from 'child_process';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { z } from 'zod';
import jwt from 'jsonwebtoken';
import jwksClient from 'jwks-rsa';
import crypto from 'crypto';

const app = express();
const PORT = process.env.PORT || 8000;
const ENABLE_AUTH = process.env.ENABLE_AUTH === 'true';
const OAUTH_ISSUER = process.env.OAUTH_ISSUER || 'https://your-idp.example.com/';
const OAUTH_AUDIENCE = process.env.OAUTH_AUDIENCE || 'https://mcp.example.com';
const JWKS_URI = process.env.JWKS_URI || `${OAUTH_ISSUER}.well-known/jwks.json`;

const client = jwksClient({ jwksUri: JWKS_URI });
function getKey(header, callback) {
  client.getSigningKey(header.kid, function (err, key) {
    if (err) return callback(err);
    const signingKey = key.publicKey || key.rsaPublicKey;
    callback(null, signingKey);
  });
}

// OAuth 2.1 Middleware
const authMiddleware = (req, res, next) => {
  if (req.path === '/health' || !ENABLE_AUTH) {
    return next();
  }
  const authHeader = req.headers.authorization;
  if (!authHeader || !authHeader.startsWith('Bearer ')) {
    res.setHeader('WWW-Authenticate', 'Bearer');
    return res.status(401).json({ error: 'Missing or invalid Authorization header' });
  }
  const token = authHeader.split(' ')[1];
  jwt.verify(token, getKey, { audience: OAUTH_AUDIENCE, issuer: OAUTH_ISSUER }, (err, decoded) => {
    if (err) {
      res.setHeader('WWW-Authenticate', `Bearer error="invalid_token", error_description="${err.message}"`);
      return res.status(401).json({ error: 'Invalid token' });
    }
    req.user = decoded;
    next();
  });
};

app.use(authMiddleware);

app.get('/health', (req, res) => res.json({ status: 'ok', service: 'gws-mcp-server-node' }));

const server = new McpServer({
  name: "GWS Remote MCP Server",
  version: "1.0.0"
});

server.tool("execute_gws", {
  service: z.string().describe("The GWS service to use (e.g., 'gmail')"),
  command: z.string().describe("The subcommand to execute (e.g., 'messages list')"),
  params: z.record(z.any()).optional().describe("Optional JSON parameters (--params flag)"),
  args: z.array(z.string()).optional().describe("Optional list of additional args")
}, async ({ service, command, params, args }) => {
  return new Promise((resolve) => {
    const cmdArgs = [service, ...command.split(' ')];
    if (params) cmdArgs.push('--params', JSON.stringify(params));
    if (args) cmdArgs.push(...args);

    const proc = spawn('gws', cmdArgs);
    let stdout = '';
    let stderr = '';

    proc.stdout.on('data', (data) => { stdout += data.toString(); });
    proc.stderr.on('data', (data) => { stderr += data.toString(); });

    proc.on('close', (code) => {
      if (code !== 0) {
        resolve({
          content: [{ type: "text", text: `Error (Exit Code ${code}):\n${stderr}` }]
        });
      } else {
        resolve({
          content: [{ type: "text", text: stdout }]
        });
      }
    });
  });
});

const transport = new StreamableHTTPServerTransport();

// Create a unified endpoint for Streamable HTTP
app.all("/mcp", async (req, res) => {
  try {
    await transport.handleRequest(req, res, server);
  } catch(e) {
      console.error(e);
  }
});

app.listen(PORT, () => {
  console.log(`GWS Remote MCP Server listening on port ${PORT}`);
});
