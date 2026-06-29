import express from 'express';
import { spawn } from 'child_process';
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { z } from 'zod';
import jwt from 'jsonwebtoken';
import jwksClient from 'jwks-rsa';

const app = express();
const PORT = process.env.PORT || 8000;
const ENABLE_AUTH = process.env.ENABLE_AUTH === 'true';
const OAUTH_ISSUER = process.env.OAUTH_ISSUER || 'https://your-idp.example.com/';
const OAUTH_AUDIENCE = process.env.OAUTH_AUDIENCE || 'https://mcp.example.com';
const JWKS_URI = process.env.JWKS_URI || `${OAUTH_ISSUER}.well-known/jwks.json`;

// --- Scope / service restriction ---
//
// GWS_ALLOWED_SERVICES: comma-separated list of gws service names that clients
// may invoke through this MCP server (e.g. "drive,gmail,calendar").
// Omit the variable (or set it to "*") to allow all services.
const _allowedServicesEnv = (process.env.GWS_ALLOWED_SERVICES || '').trim();
const ALLOWED_SERVICES = (
  _allowedServicesEnv && _allowedServicesEnv !== '*'
    ? new Set(_allowedServicesEnv.split(',').map(s => s.trim()).filter(Boolean))
    : null  // null = no restriction
);

if (ALLOWED_SERVICES) {
  console.log(`Service allowlist active: ${[...ALLOWED_SERVICES].sort().join(', ')}`);
} else {
  console.log("Service allowlist: unrestricted (GWS_ALLOWED_SERVICES not set or '*')");
}

// JWKS client for JWT signature verification
const jwks = jwksClient({ jwksUri: JWKS_URI });
function getKey(header, callback) {
  jwks.getSigningKey(header.kid, (err, key) => {
    if (err) return callback(err);
    callback(null, key.publicKey || key.rsaPublicKey);
  });
}

// OAuth 2.1 middleware
const authMiddleware = (req, res, next) => {
  if (req.path === '/health' || !ENABLE_AUTH) return next();

  const authHeader = req.headers.authorization;
  if (!authHeader || !authHeader.startsWith('Bearer ')) {
    res.setHeader('WWW-Authenticate', 'Bearer');
    return res.status(401).json({ error: 'Missing or invalid Authorization header' });
  }

  const token = authHeader.split(' ')[1];
  jwt.verify(token, getKey, { audience: OAUTH_AUDIENCE, issuer: OAUTH_ISSUER }, (err, decoded) => {
    if (err) {
      res.setHeader(
        'WWW-Authenticate',
        `Bearer error="invalid_token", error_description="${err.message}"`
      );
      return res.status(401).json({ error: 'Invalid token' });
    }
    req.user = decoded;
    next();
  });
};

app.use(authMiddleware);

app.get('/health', (_req, res) => res.json({ status: 'ok', service: 'gws-mcp-server-node' }));

const server = new McpServer({
  name: 'GWS Remote MCP Server',
  version: '1.0.0',
});

server.tool(
  'execute_gws',
  {
    service: z.string().describe("The gws service to invoke (e.g. 'gmail', 'drive')"),
    command: z.string().describe("Sub-command string (e.g. 'messages list', 'files get')"),
    params: z.record(z.any()).optional().describe('Optional JSON parameters (--params flag)'),
    args: z.array(z.string()).optional().describe('Optional additional raw CLI arguments'),
  },
  async ({ service, command, params, args }) => {
    // Enforce service allowlist before spawning a subprocess.
    if (ALLOWED_SERVICES && !ALLOWED_SERVICES.has(service)) {
      const allowed = [...ALLOWED_SERVICES].sort().join(', ');
      return {
        content: [{
          type: 'text',
          text: `Error: Service '${service}' is not permitted on this server.\nAllowed services: ${allowed}`,
        }],
      };
    }

    const cmdArgs = [service, ...command.split(' ')];
    if (params) cmdArgs.push('--params', JSON.stringify(params));
    if (args) cmdArgs.push(...args);

    return new Promise((resolve) => {
      const proc = spawn('gws', cmdArgs);
      let stdout = '';
      let stderr = '';

      proc.stdout.on('data', (data) => { stdout += data.toString(); });
      proc.stderr.on('data', (data) => { stderr += data.toString(); });

      proc.on('error', (err) => {
        if (err.code === 'ENOENT') {
          resolve({
            content: [{
              type: 'text',
              text: "Error: 'gws' binary not found. Install it and ensure it is on PATH.",
            }],
          });
        } else {
          resolve({ content: [{ type: 'text', text: `Process error: ${err.message}` }] });
        }
      });

      proc.on('close', (code) => {
        if (code !== 0) {
          resolve({ content: [{ type: 'text', text: `Error (Exit Code ${code}):\n${stderr}` }] });
        } else {
          resolve({ content: [{ type: 'text', text: stdout }] });
        }
      });
    });
  }
);

const transport = new StreamableHTTPServerTransport();

app.all('/mcp', async (req, res) => {
  try {
    await transport.handleRequest(req, res, server);
  } catch (e) {
    console.error(e);
  }
});

app.listen(PORT, () => {
  console.log(`GWS Remote MCP Server listening on port ${PORT}`);
});
