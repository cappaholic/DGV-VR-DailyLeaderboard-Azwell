// api/trigger-snapshot.js
// Called by Vercel cron at 23:55 UTC daily.
// Dispatches the GitHub Actions snapshot workflow via the GitHub API.
export default async function handler(req, res) {
  // Only allow GET (from cron) or POST
  if (req.method !== 'GET' && req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed' });
  }

  const pat   = process.env.GITHUB_PAT;
  const owner = 'cappaholic';
  const repo  = 'DGV-VR-DailyLeaderboard-Azwell';

  if (!pat) {
    console.error('[trigger-snapshot] GITHUB_PAT not set');
    return res.status(500).json({ error: 'GITHUB_PAT not configured' });
  }

  try {
    const response = await fetch(
      `https://api.github.com/repos/${owner}/${repo}/actions/workflows/snapshot-leaderboard.yml/dispatches`,
      {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${pat}`,
          'Accept':        'application/vnd.github+json',
          'Content-Type':  'application/json',
          'X-GitHub-Api-Version': '2022-11-28',
        },
        body: JSON.stringify({ ref: 'main' }),
      }
    );

    if (response.status === 204) {
      console.log('[trigger-snapshot] Workflow dispatched successfully');
      return res.status(200).json({ ok: true, message: 'Snapshot workflow triggered' });
    } else {
      const body = await response.text();
      console.error('[trigger-snapshot] GitHub API error:', response.status, body);
      return res.status(500).json({ error: 'GitHub dispatch failed', status: response.status, body });
    }
  } catch (err) {
    console.error('[trigger-snapshot] Error:', err.message);
    return res.status(500).json({ error: err.message });
  }
}
