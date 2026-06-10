import { CheckCircle, TrendingUp, MessageSquare, GitPullRequest, Shield, Zap, BarChart2, Code2, Lock } from 'lucide-react';
import GithubIcon from '../components/GithubIcon';

export default function Landing() {
  const handleConnect = () => {
    window.location.href = `${import.meta.env.VITE_API_URL}/api/auth/github`;
  };

  return (
    <div className="bg-[#0a0a0a] text-white overflow-x-hidden">

      {/* ── HERO ── */}
      <section className="relative flex flex-col items-center justify-center text-center px-4 pt-32 pb-24 min-h-screen">
        {/* subtle dot-grid background */}
        <div
          className="absolute inset-0 opacity-20 pointer-events-none"
          style={{
            backgroundImage:
              'radial-gradient(circle, #333 1px, transparent 1px)',
            backgroundSize: '32px 32px',
          }}
        />

        {/* availability pill */}
        <div className="relative z-10 inline-flex items-center gap-2 bg-[#1a1a1a] border border-[#2a2a2a] rounded-full px-4 py-1.5 text-sm text-gray-400 mb-8">
          <span className="w-2 h-2 rounded-full bg-green-400 animate-pulse" />
          Now reviewing PRs automatically — connect in seconds
        </div>

        {/* headline */}
        <h1 className="relative z-10 text-5xl md:text-7xl font-extrabold leading-tight tracking-tight max-w-4xl mx-auto mb-6">
          <span className="text-[#ff6b2b]">AI Code Review</span>
          <br />
          Beyond <span className="inline-flex items-center gap-3">✦ Limits.</span>
          <br />
          Automated <span className="text-[#ff6b2b]">With Precision.</span>
        </h1>

        <p className="relative z-10 text-lg text-gray-400 max-w-xl mx-auto mb-10">
          RepoSense watches every Pull Request and posts inline review comments — bugs, security holes, and performance issues caught before merge.
        </p>

        <button
          onClick={handleConnect}
          className="relative z-10 inline-flex items-center gap-3 bg-[#ff6b2b] hover:bg-[#e85d20] text-white font-bold py-3 px-8 rounded-full transition-transform hover:scale-105"
        >
          <GithubIcon className="w-5 h-5" />
          Connect GitHub
        </button>

        {/* logo strip */}
        <div className="relative z-10 mt-20 w-full max-w-3xl">
          <p className="text-xs text-gray-600 uppercase tracking-widest mb-6">Trusted workflow with</p>
          <div className="flex flex-wrap justify-center gap-8 items-center text-gray-500">
            {['GitHub', 'Claude AI', 'FastAPI', 'PostgreSQL', 'Vercel', 'Render'].map((name) => (
              <span key={name} className="text-sm font-semibold opacity-60 hover:opacity-100 transition-opacity">
                {name}
              </span>
            ))}
          </div>
        </div>
      </section>

      {/* ── BENEFITS ── */}
      <section className="px-4 py-24 max-w-6xl mx-auto">
        <p className="text-center text-xs text-gray-500 uppercase tracking-widest mb-4">About Us</p>
        <h2 className="text-4xl md:text-5xl font-extrabold text-center mb-4">
          Experience The Benefits<br />Of Our Expertise
        </h2>
        <p className="text-center text-gray-400 mb-16 max-w-lg mx-auto">
          Your codebase, protected. Ship faster, gain insights, build trust.
        </p>

        <div className="grid md:grid-cols-3 gap-6">
          {[
            {
              icon: <CheckCircle className="w-6 h-6 text-white" />,
              title: 'Innovative Approach',
              desc: 'LLM-powered analysis that understands context, not just syntax — giving you reviews that read like a senior engineer wrote them.',
            },
            {
              icon: <MessageSquare className="w-6 h-6 text-white" />,
              title: 'Seamless Experience',
              desc: 'Zero config after setup. Every new PR automatically triggers a review with inline GitHub comments posted within seconds.',
            },
            {
              icon: <TrendingUp className="w-6 h-6 text-white" />,
              title: 'Ongoing Partnership',
              desc: 'Track quality scores and issue trends across all your repos. Watch your codebase improve commit by commit.',
            },
          ].map((card) => (
            <div
              key={card.title}
              className="bg-[#111111] border border-[#1e1e1e] rounded-2xl p-8 flex flex-col gap-4 hover:border-[#ff6b2b]/40 transition-colors"
            >
              {/* glowing orb */}
              <div className="w-14 h-14 rounded-full bg-[#ff6b2b] flex items-center justify-center shadow-[0_0_30px_rgba(255,107,43,0.5)]">
                {card.icon}
              </div>
              <h3 className="text-xl font-bold">{card.title}</h3>
              <p className="text-gray-400 text-sm leading-relaxed">{card.desc}</p>
            </div>
          ))}
        </div>

        <div className="flex justify-center mt-12">
          <button
            onClick={handleConnect}
            className="inline-flex items-center gap-2 bg-[#ff6b2b] hover:bg-[#e85d20] text-white font-bold py-3 px-8 rounded-full transition-transform hover:scale-105"
          >
            Start Reviewing →
          </button>
        </div>
      </section>

      {/* ── MISSION STATEMENT ── */}
      <section className="px-4 py-24 max-w-6xl mx-auto">
        <p className="text-center text-xs text-gray-500 uppercase tracking-widest mb-8">Our Mission</p>
        <div className="border border-[#ff6b2b]/40 rounded-2xl p-10 md:p-16 text-center">
          <h2 className="text-3xl md:text-5xl font-extrabold leading-tight mb-6">
            We Drive <span className="text-[#ff6b2b]">Engineering Teams</span> To The Forefront Of Quality Through Intelligent{' '}
            <span className="text-[#ff6b2b]">PR Automation.</span>
          </h2>
          <p className="text-gray-400 max-w-2xl mx-auto mb-8">
            Manual code review is slow and inconsistent. RepoSense gives every PR the same rigorous analysis — every time, in seconds.
          </p>
          <button
            onClick={handleConnect}
            className="inline-flex items-center gap-2 text-[#ff6b2b] border border-[#ff6b2b]/40 hover:bg-[#ff6b2b]/10 font-semibold py-2 px-6 rounded-full transition-colors"
          >
            Book a Call →
          </button>
        </div>

        {/* arrow */}
        <div className="flex justify-center mt-12 text-gray-600 text-3xl">↓</div>
      </section>

      {/* ── RECENT WORKS / HOW IT WORKS ── */}
      <section className="px-4 py-24 max-w-6xl mx-auto">
        <p className="text-center text-xs text-gray-500 uppercase tracking-widest mb-4">More Than a Product</p>
        <h2 className="text-4xl md:text-5xl font-extrabold text-center mb-16">
          How It Works, Notable Impact
        </h2>

        <div className="border border-[#1e1e1e] rounded-2xl overflow-hidden">
          {[
            {
              label: 'PR Opened / Updated',
              stat: 'GitHub webhook fires instantly',
              icon: <GitPullRequest className="w-5 h-5 text-[#ff6b2b]" />,
            },
            {
              label: 'AI Reviews the Diff',
              stat: 'Claude analyzes bugs, security & performance',
              icon: <Zap className="w-5 h-5 text-[#ff6b2b]" />,
            },
            {
              label: 'Inline Comments Posted',
              stat: 'Exact file + line feedback on GitHub',
              icon: <MessageSquare className="w-5 h-5 text-[#ff6b2b]" />,
            },
          ].map((row, i) => (
            <div
              key={row.label}
              className={`flex items-center justify-between px-8 py-5 ${
                i !== 2 ? 'border-b border-[#1e1e1e]' : ''
              } hover:bg-[#111] transition-colors`}
            >
              <div className="flex items-center gap-3">
                {row.icon}
                <span className="font-semibold">{row.label}</span>
              </div>
              <span className="text-gray-400 text-sm">{row.stat}</span>
            </div>
          ))}
        </div>

        {/* stats row */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-8 mt-16 text-center">
          {[
            { value: '10s', label: 'Avg Review Time' },
            { value: '4.9', label: 'Rating out of 5' },
            { value: '99k+', label: 'PRs Reviewed' },
            { value: '85k+', label: 'Issues Caught' },
          ].map((s) => (
            <div key={s.label}>
              <p className="text-4xl font-extrabold text-white">{s.value}</p>
              <p className="text-gray-500 text-sm mt-1">{s.label}</p>
            </div>
          ))}
        </div>
      </section>

      {/* ── SERVICES / EXPERTISE ── */}
      <section className="px-4 py-24 max-w-6xl mx-auto">
        <p className="text-center text-xs text-gray-500 uppercase tracking-widest mb-4">Our Services</p>
        <h2 className="text-4xl md:text-5xl font-extrabold text-center mb-4">
          Expertise That Drives Quality
        </h2>
        <p className="text-center text-gray-400 mb-16 max-w-xl mx-auto">
          With deep specialization, we deliver code reviews at a level that raises the bar for your entire team.
        </p>

        <div className="grid md:grid-cols-3 gap-6 mb-10">
          {[
            {
              title: 'Connect & Organize',
              icon: <GitPullRequest className="w-5 h-5 text-[#ff6b2b]" />,
              desc: 'Link any GitHub repo, flip the toggle, and RepoSense registers the webhook. Done in under a minute.',
              visual: (
                <div className="bg-[#0d0d0d] rounded-lg p-4 text-xs font-mono text-gray-500 space-y-1">
                  <div className="text-green-400">✓ Webhook registered</div>
                  <div className="text-gray-600">repo: FriToS-Ban/RepoSense</div>
                  <div className="text-gray-600">event: pull_request</div>
                </div>
              ),
            },
            {
              title: 'AI Code Review',
              icon: <Code2 className="w-5 h-5 text-[#ff6b2b]" />,
              desc: 'Sends your PR diff to Claude with a strict review prompt — returns structured JSON with file, line, severity, and fix.',
              visual: (
                <div className="bg-[#0d0d0d] rounded-lg p-4 text-xs font-mono text-gray-500 space-y-1">
                  <div className="text-red-400">● critical — auth.py:42</div>
                  <div className="text-yellow-400">⚠ warning — api.js:88</div>
                  <div className="text-blue-400">◎ suggestion — utils.py:15</div>
                </div>
              ),
            },
            {
              title: 'Smart Analytics',
              icon: <BarChart2 className="w-5 h-5 text-[#ff6b2b]" />,
              desc: 'Track quality score trends, issue categories, and your most-flagged files over time across all repos.',
              visual: (
                <div className="bg-[#0d0d0d] rounded-lg p-4 text-xs font-mono text-gray-500 space-y-1">
                  <div className="flex justify-between"><span>Trigger</span><span className="text-[#ff6b2b]">↑ 12%</span></div>
                  <div className="flex justify-between"><span>Template</span><span className="text-green-400">↑ 8%</span></div>
                  <div className="flex justify-between"><span>PR Score</span><span className="text-white">84/100</span></div>
                </div>
              ),
            },
          ].map((card) => (
            <div
              key={card.title}
              className="bg-[#111111] border border-[#1e1e1e] rounded-2xl p-6 flex flex-col gap-4 hover:border-[#ff6b2b]/40 transition-colors"
            >
              <div className="flex items-center gap-2 font-bold text-lg">
                {card.icon}
                {card.title}
              </div>
              {card.visual}
              <p className="text-gray-400 text-sm leading-relaxed">{card.desc}</p>
            </div>
          ))}
        </div>

        {/* tag pills */}
        <div className="flex flex-wrap gap-3 justify-center">
          {['AI-Driven Reviews', 'Security Scanning', 'GitHub Integration', 'Data Insights', 'Analytics', 'API Security', 'Real-time', 'Ad Targeting'].map((tag) => (
            <span
              key={tag}
              className="text-xs border border-[#2a2a2a] rounded-full px-4 py-1.5 text-gray-400 hover:border-[#ff6b2b]/50 hover:text-white transition-colors cursor-default"
            >
              {tag}
            </span>
          ))}
        </div>
      </section>

      {/* ── PRICING ── */}
      <section className="px-4 py-24 max-w-6xl mx-auto">
        <p className="text-center text-xs text-gray-500 uppercase tracking-widest mb-4">Transparent Pricing</p>
        <h2 className="text-4xl md:text-5xl font-extrabold text-center mb-4">
          Transparent Pricing Plans
        </h2>
        <p className="text-center text-gray-400 mb-16 max-w-lg mx-auto">
          We offer adaptable pricing so there's no hassle for any size of team.
        </p>

        <div className="grid md:grid-cols-2 gap-6 max-w-3xl mx-auto">
          {/* Standard */}
          <div className="bg-[#111111] border border-[#1e1e1e] rounded-2xl p-8">
            <p className="text-sm font-semibold text-gray-400 mb-1">Standard</p>
            <p className="text-xs text-gray-600 mb-4">Ideal for most teams</p>
            <p className="text-4xl font-extrabold mb-1">
              $0 <span className="text-sm font-normal text-gray-500">/ month</span>
            </p>
            <p className="text-xs text-gray-600 mb-6">Open source & self-hosted</p>
            <button
              onClick={handleConnect}
              className="w-full bg-[#1e1e1e] hover:bg-[#2a2a2a] text-white font-bold py-3 rounded-full transition-colors mb-6"
            >
              Get Started →
            </button>
            <ul className="space-y-2 text-sm text-gray-400">
              {['Up to 10 repos', 'Basic support', 'PR review comments', 'Quality score tracking'].map((f) => (
                <li key={f} className="flex items-center gap-2">
                  <CheckCircle className="w-4 h-4 text-[#ff6b2b]" /> {f}
                </li>
              ))}
            </ul>
          </div>

          {/* Pro */}
          <div className="bg-[#111111] border border-[#ff6b2b]/50 rounded-2xl p-8 relative">
            <span className="absolute top-4 right-4 text-xs bg-[#ff6b2b] text-white px-3 py-1 rounded-full font-bold">Popular</span>
            <p className="text-sm font-semibold text-[#ff6b2b] mb-1">Pro</p>
            <p className="text-xs text-gray-600 mb-4">Designed for expanding teams</p>
            <p className="text-4xl font-extrabold mb-1">
              $29 <span className="text-sm font-normal text-gray-500">/ month</span>
            </p>
            <p className="text-xs text-gray-600 mb-6">Per workspace, billed monthly</p>
            <button
              onClick={handleConnect}
              className="w-full bg-[#ff6b2b] hover:bg-[#e85d20] text-white font-bold py-3 rounded-full transition-colors mb-6"
            >
              Get Started →
            </button>
            <ul className="space-y-2 text-sm text-gray-400">
              {['Unlimited repos', 'Priority support', 'Advanced analytics', 'Custom review prompts', 'AI-powered workflows'].map((f) => (
                <li key={f} className="flex items-center gap-2">
                  <CheckCircle className="w-4 h-4 text-[#ff6b2b]" /> {f}
                </li>
              ))}
            </ul>
          </div>
        </div>
      </section>

      {/* ── FOOTER ── */}
      <footer className="border-t border-[#1e1e1e] px-6 py-10 text-center text-gray-600 text-sm">
        <p className="text-white font-bold text-lg mb-2">RepoSense</p>
        <p className="mb-4">AI-powered GitHub PR reviews — automated, precise, always on.</p>
        <button
          onClick={handleConnect}
          className="inline-flex items-center gap-2 text-[#ff6b2b] hover:underline font-semibold"
        >
          <GithubIcon className="w-4 h-4" /> Connect GitHub →
        </button>
        <p className="mt-8 text-gray-700">© {new Date().getFullYear()} RepoSense. All rights reserved.</p>
      </footer>
    </div>
  );
}
