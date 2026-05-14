// api/leaderboard.js  — GET /api/leaderboard/*
// Proxies requests to the Unity Leaderboards API.

export default async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin',  '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization, ProjectId');

  if (req.method === 'OPTIONS') return res.status(200).end();
  if (req.method !== 'GET')    return res.status(405).json({ error: 'Method not allowed' });

  const PROJECT_ID = process.env.UNITY_PROJECT_ID;

  // Strip /api/leaderboard prefix and forward the rest to Unity
  const suffix = req.url.replace(/^\/api\/leaderboard/, '');
  const url    = `https://leaderboards.services.api.unity.com${suffix}`;

  console.log(`[leaderboard] GET ${url}`);

  try {
    const upstream = await fetch(url, {
      method:  'GET',
      headers: {
        'Authorization': req.headers['authorization'] || '',
        'ProjectId':     PROJECT_ID,
      },
    });
    const data = await upstream.json();
    return res.status(upstream.status).json(data);
  } catch (err) {
    console.error('[leaderboard] Error:', err);
    return res.status(500).json({ error: 'Leaderboard proxy error', detail: err.message });
  }
}
