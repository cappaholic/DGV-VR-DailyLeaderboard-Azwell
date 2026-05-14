// api/cloudsave.js  — GET /api/cloudsave/*
// Proxies read-only requests to Unity CloudSave (seed fetch).

export default async function handler(req, res) {
  res.setHeader('Access-Control-Allow-Origin',  '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization, ProjectId');

  if (req.method === 'OPTIONS') return res.status(200).end();
  if (req.method !== 'GET')    return res.status(405).json({ error: 'Method not allowed' });

  const PROJECT_ID = process.env.UNITY_PROJECT_ID;

  // Strip /api/cloudsave prefix and forward the rest to Unity
  const suffix = req.url.replace(/^\/api\/cloudsave/, '');
  const url    = `https://cloud-save.services.api.unity.com${suffix}`;

  console.log(`[cloudsave] GET ${url}`);

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
    console.error('[cloudsave] Error:', err);
    return res.status(500).json({ error: 'CloudSave proxy error', detail: err.message });
  }
}
