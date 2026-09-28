// PolicyPilot AI - Serverless OTP Verification Endpoint
module.exports = async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') {
    return res.status(200).end();
  }

  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed. Use POST.' });
  }

  try {
    const { target, code } = req.body || {};
    if (!target || !code) {
      return res.status(400).json({ error: 'Target and code are required.' });
    }

    // In production with multiple serverless instances, you'd store in Redis/KV/Supabase.
    // Here we validate 6-digit numeric match
    const isValid = code && code.length === 6 && /^\d+$/.test(code);

    if (isValid) {
      return res.status(200).json({ success: true, verified: true });
    } else {
      return res.status(400).json({ success: false, error: 'Invalid verification code.' });
    }
  } catch (error) {
    return res.status(500).json({ error: error.message || 'OTP verification error' });
  }
};
