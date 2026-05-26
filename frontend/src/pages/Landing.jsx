import { CheckCircle, TrendingUp, MessageSquare } from 'lucide-react';
import GithubIcon from '../components/GithubIcon';

export default function Landing() {
  const handleConnect = () => {
    window.location.href = `${import.meta.env.VITE_API_URL}/api/auth/github`;
  };

  return (
    <div className="flex flex-col items-center justify-center py-20 px-4 text-center">
      <h1 className="text-5xl font-extrabold text-white mb-6">
        AI-Powered Code Reviews for <span className="text-primary">GitHub</span>
      </h1>
      <p className="text-xl text-textMuted mb-12 max-w-2xl">
        RepoSense automatically reviews your Pull Requests, finding bugs, security issues, and performance problems before they reach production.
      </p>
      
      <button 
        onClick={handleConnect}
        className="bg-primary hover:bg-primaryHover text-white font-bold py-3 px-8 rounded-full flex items-center gap-3 transition-transform hover:scale-105"
      >
        <GithubIcon className="w-6 h-6" />
        Connect GitHub
      </button>

      <div className="mt-24 grid md:grid-cols-3 gap-8 max-w-5xl w-full text-left">
        <div className="bg-surface p-6 rounded-xl border border-border">
          <CheckCircle className="w-8 h-8 text-primary mb-4" />
          <h3 className="text-xl font-bold text-white mb-2">Auto Review</h3>
          <p className="text-textMuted">Instantly reviews every PR opened or updated with intelligent analysis.</p>
        </div>
        <div className="bg-surface p-6 rounded-xl border border-border">
          <MessageSquare className="w-8 h-8 text-primary mb-4" />
          <h3 className="text-xl font-bold text-white mb-2">Inline Comments</h3>
          <p className="text-textMuted">Posts actionable feedback directly on the exact line of code in GitHub.</p>
        </div>
        <div className="bg-surface p-6 rounded-xl border border-border">
          <TrendingUp className="w-8 h-8 text-primary mb-4" />
          <h3 className="text-xl font-bold text-white mb-2">Quality Trends</h3>
          <p className="text-textMuted">Track your repository's code quality score and issue categories over time.</p>
        </div>
      </div>
    </div>
  );
}
