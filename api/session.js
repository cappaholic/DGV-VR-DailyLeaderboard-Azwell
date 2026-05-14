// api/session.js  — GET /api/session
// Returns the session token stored in the UNITY_SESSION_TOKEN environment variable.
// In Vercel, set this via: Project Settings → Environment Variables

export default function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin',  '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') return res.status(200).end();
  if (req.method !== 'GET')    return res.status(405).json({ error: 'Method not allowed' });

  const token = process.env.UNITY_SESSION_TOKEN || '';

  if (!token) {
    console.warn('[session] UNITY_SESSION_TOKEN environment variable is not set');
    return res.status(200).json({ found: false, token: '' });
  }

  return res.status(200).json({ found: true, token: token.trim() });
}
