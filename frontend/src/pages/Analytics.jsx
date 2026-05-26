import { useState, useEffect } from 'react';
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, BarChart, Bar, Cell } from 'recharts';

const API = import.meta.env.VITE_API_URL;

const COLORS = {
  security: '#f85149',
  performance: '#d29922',
  logic: '#388bfd',
  style: '#8b949e',
  documentation: '#3fb950',
};

export default function Analytics() {
  const [overview, setOverview] = useState(null);
  const [categories, setCategories] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    const fetchData = async () => {
      try {
        const [overviewRes, categoriesRes] = await Promise.all([
          fetch(`${API}/api/analytics/overview`, { credentials: 'include' }),
          fetch(`${API}/api/analytics/categories`, { credentials: 'include' })
        ]);

        if (!overviewRes.ok || !categoriesRes.ok) {
          throw new Error('Failed to fetch analytics');
        }

        const overviewData = await overviewRes.json();
        const categoriesData = await categoriesRes.json();

        setOverview(overviewData);
        setCategories(categoriesData);
      } catch (err) {
        setError('Failed to load analytics data');
      } finally {
        setLoading(false);
      }
    };

    fetchData();
  }, []);

  if (loading) return <div className="text-white text-center py-20">Loading...</div>;
  if (error) return <div className="text-red-400 text-center py-20">{error}</div>;

  return (
    <div className="max-w-6xl mx-auto py-8 px-4 text-white">
      <h2 className="text-3xl font-bold mb-8">Code Quality Trends</h2>

      {/* Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
        <div className="bg-surface border border-border rounded-xl p-4 text-center">
          <div className="text-3xl font-bold text-primary">{overview?.total_prs ?? 0}</div>
          <div className="text-textMuted text-sm mt-1">Total PRs Reviewed</div>
        </div>
        <div className="bg-surface border border-border rounded-xl p-4 text-center">
          <div className="text-3xl font-bold text-primary">{overview?.average_score ?? 0}</div>
          <div className="text-textMuted text-sm mt-1">Avg Quality Score</div>
        </div>
        <div className="bg-surface border border-border rounded-xl p-4 text-center">
          <div className="text-3xl font-bold text-red-400">{overview?.total_critical ?? 0}</div>
          <div className="text-textMuted text-sm mt-1">Critical Issues</div>
        </div>
        <div className="bg-surface border border-border rounded-xl p-4 text-center">
          <div className="text-3xl font-bold text-yellow-400">{overview?.total_warnings ?? 0}</div>
          <div className="text-textMuted text-sm mt-1">Warnings</div>
        </div>
      </div>

      <div className="grid md:grid-cols-2 gap-8">
        {/* Quality Score Chart */}
        <div className="bg-surface p-6 rounded-xl border border-border">
          <h3 className="text-xl font-bold mb-4">Quality Score (Last 30 Days)</h3>
          <div className="h-64">
            {overview?.trends?.length > 0 ? (
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={overview.trends}>
                  <XAxis dataKey="date" stroke="#8b949e" tick={{ fontSize: 11 }} />
                  <YAxis stroke="#8b949e" domain={[0, 100]} />
                  <Tooltip contentStyle={{ backgroundColor: '#161b22', border: '1px solid #30363d' }} />
                  <Line type="monotone" dataKey="score" stroke="#238636" strokeWidth={3} dot={{ r: 4 }} />
                </LineChart>
              </ResponsiveContainer>
            ) : (
              <div className="h-full flex items-center justify-center text-textMuted">No data yet</div>
            )}
          </div>
        </div>

        {/* Issues by Category */}
        <div className="bg-surface p-6 rounded-xl border border-border">
          <h3 className="text-xl font-bold mb-4">Issues by Category</h3>
          <div className="h-64">
            {categories?.length > 0 ? (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={categories}>
                  <XAxis dataKey="category" stroke="#8b949e" tick={{ fontSize: 11 }} />
                  <YAxis stroke="#8b949e" />
                  <Tooltip contentStyle={{ backgroundColor: '#161b22', border: '1px solid #30363d' }} />
                  <Bar dataKey="count" radius={[4, 4, 0, 0]}>
                    {categories.map((entry) => (
                      <Cell key={entry.category} fill={COLORS[entry.category] || '#8b949e'} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <div className="h-full flex items-center justify-center text-textMuted">No data yet</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}