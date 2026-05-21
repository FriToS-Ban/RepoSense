import { Link } from 'react-router-dom';
import { LogOut, BarChart2 } from 'lucide-react';
import GithubIcon from './GithubIcon';

export default function Navbar() {
  const handleLogout = async () => {
    await fetch('http://localhost:8000/api/auth/logout', { method: 'POST', credentials: 'omit' });
    // In real app, we'd want to handle credentials properly and update context state
    window.location.href = '/';
  };

  return (
    <nav className="border-b border-border bg-surface px-6 py-4 flex justify-between items-center">
      <Link to="/" className="text-xl font-bold text-white flex items-center gap-2">
        <GithubIcon className="w-6 h-6" />
        RepoSense
      </Link>
      <div className="flex gap-4 items-center">
        <Link to="/dashboard" className="text-textMain hover:text-white transition font-medium">Dashboard</Link>
        <Link to="/analytics" className="text-textMain hover:text-white transition flex items-center gap-1 font-medium">
          <BarChart2 className="w-4 h-4" /> Analytics
        </Link>
        <button onClick={handleLogout} className="text-textMuted hover:text-danger transition" title="Logout">
          <LogOut className="w-5 h-5" />
        </button>
      </div>
    </nav>
  );
}
