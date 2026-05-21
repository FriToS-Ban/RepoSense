import { useState, useEffect } from 'react';
import { Link } from 'react-router-dom';

export default function Dashboard() {
  const [repos, setRepos] = useState([]);
  const [prs, setPrs] = useState([]);

  useEffect(() => {
    // In real app, fetch these from API
    setRepos([
      { github_repo_id: '1', repo_name: 'frontend-app', repo_full_name: 'user/frontend-app', is_active: true },
      { github_repo_id: '2', repo_name: 'backend-api', repo_full_name: 'user/backend-api', is_active: false }
    ]);
    setPrs([
      { id: '1', repo_full_name: 'user/frontend-app', title: 'Add login page', status: 'reviewed', quality_score: 85, created_at: '2023-10-01' },
      { id: '2', repo_full_name: 'user/frontend-app', title: 'Fix CSS bugs', status: 'failed', quality_score: null, created_at: '2023-10-02' }
    ]);
  }, []);

  const handleToggle = async (repo_full_name, github_repo_id, isActive) => {
    // In real app, call /api/repos/enable or /disable
    console.log(`Toggle ${repo_full_name} to ${!isActive}`);
  };

  const handleRetry = async (pr_id) => {
    // Call /api/prs/:id/retry
    console.log(`Retry PR ${pr_id}`);
  };

  return (
    <div className="max-w-6xl mx-auto py-8 px-4">
      <h2 className="text-3xl font-bold text-white mb-8">Dashboard</h2>
      
      <div className="grid md:grid-cols-2 gap-8">
        <div>
          <h3 className="text-xl font-bold text-white mb-4">Your Repositories</h3>
          <div className="bg-surface border border-border rounded-lg p-4 space-y-4">
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
            {prs.map(pr => (
              <div key={pr.id} className="p-3 hover:bg-black/20 rounded border border-border flex justify-between items-center">
                <div>
                  <Link to={`/pr/${pr.id}`} className="font-bold text-white hover:text-primary transition">{pr.title}</Link>
                  <div className="text-sm text-textMuted">{pr.repo_full_name} • {pr.created_at}</div>
                </div>
                <div className="flex items-center gap-3">
                  {pr.status === 'failed' ? (
                    <button onClick={() => handleRetry(pr.id)} className="text-sm bg-warning text-black px-3 py-1 rounded font-bold">Retry</button>
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
