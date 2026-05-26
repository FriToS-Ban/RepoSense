import { useState, useEffect } from 'react';
import { Link, useNavigate } from 'react-router-dom';

const API = import.meta.env.VITE_API_URL;

export default function Dashboard() {
  const [repos, setRepos] = useState([]);
  const [prs, setPrs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const navigate = useNavigate();

  useEffect(() => {
    const fetchData = async () => {
      try {
        const [reposRes, prsRes] = await Promise.all([
          fetch(`${API}/api/repos`, { credentials: 'include' }),
          fetch(`${API}/api/prs`, { credentials: 'include' })
        ]);

        if (reposRes.status === 401 || prsRes.status === 401) {
          navigate('/');
          return;
        }

        const reposData = await reposRes.json();
        const prsData = await prsRes.json();

        setRepos(reposData);
        setPrs(prsData);
      } catch (err) {
        setError('Failed to load dashboard data');
      } finally {
        setLoading(false);
      }
    };

    fetchData();
  }, []);

  const handleToggle = async (repo_full_name, github_repo_id, isActive) => {
    const endpoint = isActive ? '/api/repos/disable' : '/api/repos/enable';

    let crawl_permission = false;
    if (!isActive) {
        crawl_permission = window.confirm(
            `Allow RepoSense to crawl the full codebase of ${repo_full_name} for smarter AI reviews?\n\nThis indexes your code into a knowledge graph for better context. Your code is never shared.`
        );
    }

    try {
      const res = await fetch(`${API}${endpoint}`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo_full_name, github_repo_id, crawl_permission })
      });
      if (!res.ok) throw new Error();
      setRepos(prev =>
        prev.map(r =>
          r.github_repo_id === github_repo_id ? { ...r, is_active: !isActive } : r
        )
      );
    } catch {
      alert(`Failed to ${isActive ? 'disable' : 'enable'} repository`);
    }
  };

  const handleRetry = async (pr_id) => {
    try {
      const res = await fetch(`${API}/api/prs/${pr_id}/retry`, {
        method: 'POST',
        credentials: 'include'
      });
      if (!res.ok) throw new Error();
      setPrs(prev =>
        prev.map(p => p.id === pr_id ? { ...p, status: 'pending' } : p)
      );
    } catch {
      alert('Failed to retry PR review');
    }
  };

  if (loading) return <div className="text-white text-center py-20">Loading...</div>;
  if (error) return <div className="text-red-400 text-center py-20">{error}</div>;

  return (
    <div className="max-w-6xl mx-auto py-8 px-4">
      <h2 className="text-3xl font-bold text-white mb-8">Dashboard</h2>

      <div className="grid md:grid-cols-2 gap-8">
        <div>
          <h3 className="text-xl font-bold text-white mb-4">Your Repositories</h3>
          <div className="bg-surface border border-border rounded-lg p-4 space-y-4">
            {repos.length === 0 && (
              <p className="text-textMuted text-sm">No repositories found.</p>
            )}
            {repos.map(repo => (
              <div key={repo.github_repo_id} className="flex justify-between items-center p-3 hover:bg-black/20 rounded">
                <div>
                  <div className="font-bold text-white">{repo.repo_name}</div>
                  <div className="text-sm text-textMuted">{repo.repo_full_name}</div>
                </div>
                <button
                  onClick={() => handleToggle(repo.repo_full_name, repo.github_repo_id, repo.is_active)}
                  className={`px-4 py-2 rounded font-bold text-sm ${repo.is_active ? 'bg-danger text-white' : 'bg-primary text-white'}`}
                >
                  {repo.is_active ? 'Disable' : 'Enable'}
                </button>
              </div>
            ))}
          </div>
        </div>

        <div>
          <h3 className="text-xl font-bold text-white mb-4">Recent Pull Requests</h3>
          <div className="bg-surface border border-border rounded-lg p-4 space-y-4">
            {prs.length === 0 && (
              <p className="text-textMuted text-sm">No pull requests reviewed yet.</p>
            )}
            {prs.map(pr => (
              <div key={pr.id} className="p-3 hover:bg-black/20 rounded border border-border flex justify-between items-center">
                <div>
                  <Link to={`/pr/${pr.id}`} className="font-bold text-white hover:text-primary transition">{pr.pr_title || pr.title}</Link>
                  <div className="text-sm text-textMuted">{pr.repo_full_name} • {new Date(pr.created_at).toLocaleDateString()}</div>
                </div>
                <div className="flex items-center gap-3">
                  {pr.status === 'failed' ? (
                    <button onClick={() => handleRetry(pr.id)} className="text-sm bg-warning text-black px-3 py-1 rounded font-bold">Retry</button>
                  ) : pr.status === 'pending' ? (
                    <span className="text-sm text-textMuted">Reviewing...</span>
                  ) : (
                    <span className="font-bold text-white">{pr.quality_score}/100</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}