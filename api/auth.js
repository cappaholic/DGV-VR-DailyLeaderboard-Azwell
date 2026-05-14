// api/auth.js  — POST /api/auth
// Exchanges a Unity session token for an access token.

export default async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin',  '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization, ProjectId');

  if (req.method === 'OPTIONS') return res.status(200).end();
  if (req.method !== 'POST')   return res.status(405).json({ error: 'Method not allowed' });

  const PROJECT_ID = process.env.UNITY_PROJECT_ID;

  try {
    const upstream = await fetch(
      'https://player-auth.services.api.unity.com/v1/authentication/session-token',
      {
        method:  'POST',
        headers: {
          'Content-Type':  'application/json',
          'Authorization': req.headers['authorization'] || '',
          'ProjectId':     PROJECT_ID,
        },
        body: JSON.stringify(req.body),
      }
    );
    const data = await upstream.json();
    return res.status(upstream.status).json(data);
  } catch (err) {
    console.error('[auth] Error:', err);
    return res.status(500).json({ error: 'Auth proxy error', detail: err.message });
  }
}
