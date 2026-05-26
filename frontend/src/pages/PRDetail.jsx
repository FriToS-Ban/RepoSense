import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';

const API = import.meta.env.VITE_API_URL;

const SEVERITY_COLORS = {
  critical: 'text-red-400 bg-red-400/10 border-red-400/30',
  warning: 'text-yellow-400 bg-yellow-400/10 border-yellow-400/30',
  suggestion: 'text-blue-400 bg-blue-400/10 border-blue-400/30',
};

const CATEGORY_COLORS = {
  security: 'text-red-400',
  performance: 'text-yellow-400',
  logic: 'text-blue-400',
  style: 'text-gray-400',
  documentation: 'text-green-400',
};

export default function PRDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [pr, setPr] = useState(null);
  const [comments, setComments] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    const fetchData = async () => {
      try {
        const [prRes, commentsRes] = await Promise.all([
          fetch(`${API}/api/prs/${id}`, { credentials: 'include' }),
          fetch(`${API}/api/prs/${id}/comments`, { credentials: 'include' })
        ]);

        if (prRes.status === 401) { navigate('/'); return; }
        if (!prRes.ok) throw new Error('PR not found');

        const prData = await prRes.json();
        const commentsData = await commentsRes.json();

        setPr(prData);
        setComments(commentsData);
      } catch (err) {
        setError('Failed to load PR details');
      } finally {
        setLoading(false);
      }
    };

    fetchData();
  }, [id]);

  if (loading) return <div className="text-white text-center py-20">Loading...</div>;
  if (error) return <div className="text-red-400 text-center py-20">{error}</div>;
  if (!pr) return null;

  const criticalCount = comments.filter(c => c.severity === 'critical').length;
  const warningCount = comments.filter(c => c.severity === 'warning').length;
  const suggestionCount = comments.filter(c => c.severity === 'suggestion').length;

  return (
    <div className="max-w-4xl mx-auto py-8 px-4 text-white">
      {/* Back button */}
      <button onClick={() => navigate('/dashboard')} className="text-textMuted hover:text-white mb-6 flex items-center gap-2 transition">
        ← Back to Dashboard
      </button>

      {/* PR Header */}
      <div className="bg-surface border border-border rounded-xl p-6 mb-6">
        <div className="flex justify-between items-start">
          <div>
            <h2 className="text-2xl font-bold mb-1">{pr.pr_title}</h2>
            <p className="text-textMuted text-sm">by {pr.pr_author} · {pr.repo_full_name} · PR #{pr.github_pr_number}</p>
          </div>
          {pr.quality_score !== null && (
            <div className="text-center relative group">
              <div className={`text-4xl font-bold ${pr.quality_score >= 80 ? 'text-green-400' : pr.quality_score >= 60 ? 'text-yellow-400' : 'text-red-400'}`}>
                {pr.quality_score}
              </div>
              <div className="text-textMuted text-xs flex items-center gap-1 justify-center cursor-help">
                Quality Score <span className="border border-textMuted rounded-full w-3 h-3 text-[10px] flex items-center justify-center">?</span>
              </div>
              <div className="absolute right-0 top-full mt-2 w-48 bg-gray-900 border border-border rounded-lg p-3 text-xs text-left hidden group-hover:block z-10 shadow-lg">
                <p className="font-bold text-white mb-2">Scoring Formula</p>
                <p className="text-textMuted mb-1">Starts at <span className="text-white">100</span></p>
                <p className="text-red-400 mb-1">− 15 per critical issue</p>
                <p className="text-yellow-400 mb-1">− 5 per warning</p>
                <p className="text-blue-400">− 1 per suggestion</p>
              </div>
            </div>
          )}
        </div>

        {/* Issue summary */}
        <div className="flex gap-4 mt-4">
          <span className="text-red-400 text-sm font-medium">{criticalCount} critical</span>
          <span className="text-yellow-400 text-sm font-medium">{warningCount} warnings</span>
          <span className="text-blue-400 text-sm font-medium">{suggestionCount} suggestions</span>
        </div>
      </div>

      {/* Comments */}
      <h3 className="text-xl font-bold mb-4">Review Comments ({comments.length})</h3>

      {comments.length === 0 ? (
        <div className="bg-surface border border-border rounded-xl p-6 text-textMuted text-center">
          No issues found — clean PR!
        </div>
      ) : (
        <div className="space-y-4">
          {comments.map(comment => (
            <div key={comment.id} className={`border rounded-xl p-4 ${SEVERITY_COLORS[comment.severity] || 'border-border'}`}>
              <div className="flex justify-between items-start mb-2">
                <div className="flex items-center gap-2">
                  <span className={`text-xs font-bold uppercase px-2 py-0.5 rounded ${SEVERITY_COLORS[comment.severity]}`}>
                    {comment.severity}
                  </span>
                  <span className={`text-xs font-medium ${CATEGORY_COLORS[comment.category]}`}>
                    {comment.category}
                  </span>
                </div>
                <span className="text-textMuted text-xs font-mono">
                  {comment.file_path}:{comment.line_number}
                </span>
              </div>
              <p className="text-sm text-white leading-relaxed">{comment.comment_body}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}