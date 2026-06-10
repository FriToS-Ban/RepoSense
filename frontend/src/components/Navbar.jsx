import { Link } from 'react-router-dom';
import { LogOut, BarChart2 } from 'lucide-react';
import GithubIcon from './GithubIcon';

export default function Navbar() {
  const handleLogout = async () => {
    await fetch(`${import.meta.env.VITE_API_URL}/api/auth/logout`, { method: 'POST', credentials: 'omit' });
    window.location.href = '/';
  };

  const handleConnect = () => {
    window.location.href = `${import.meta.env.VITE_API_URL}/api/auth/github`;
  };

  return (
    <nav className="fixed top-0 left-0 right-0 z-50 bg-[#0a0a0a]/80 backdrop-blur-md border-b border-[#1e1e1e] px-6 py-4 flex justify-between items-center">
      <Link to="/" className="text-lg font-extrabold text-white flex items-center gap-2 tracking-tight">
        <GithubIcon className="w-5 h-5 text-[#ff6b2b]" />
        Repo<span className="text-[#ff6b2b]">Sense</span>
      </Link>

      <div className="hidden md:flex gap-8 items-center text-sm text-gray-400">
        <Link to="/" className="hover:text-white transition-colors">Why Us</Link>
        <Link to="/dashboard" className="hover:text-white transition-colors">Dashboard</Link>
        <Link to="/analytics" className="hover:text-white transition-colors flex items-center gap-1">
          <BarChart2 className="w-3.5 h-3.5" /> Analytics
        </Link>
      </div>

      <div className="flex items-center gap-3">
        <button
          onClick={handleConnect}
          className="inline-flex items-center gap-2 bg-[#ff6b2b] hover:bg-[#e85d20] text-white text-sm font-bold py-2 px-5 rounded-full transition-colors"
        >
          <GithubIcon className="w-4 h-4" />
          Let's Talk
        </button>
        <button onClick={handleLogout} className="text-gray-600 hover:text-red-400 transition-colors" title="Logout">
          <LogOut className="w-4 h-4" />
        </button>
      </div>
    </nav>
  );
}

