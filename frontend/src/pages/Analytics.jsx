import { useState, useEffect } from 'react';
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, BarChart, Bar } from 'recharts';

export default function Analytics() {
  const data = [
    { name: 'Mon', score: 85 },
    { name: 'Tue', score: 92 },
    { name: 'Wed', score: 78 },
    { name: 'Thu', score: 95 },
  ];

  return (
    <div className="max-w-6xl mx-auto py-8 px-4 text-white">
      <h2 className="text-3xl font-bold mb-8">Code Quality Trends</h2>
      
      <div className="grid md:grid-cols-2 gap-8">
        <div className="bg-surface p-6 rounded-xl border border-border">
          <h3 className="text-xl font-bold mb-4">Quality Score (Last 30 Days)</h3>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={data}>
                <XAxis dataKey="name" stroke="#8b949e" />
                <YAxis stroke="#8b949e" />
                <Tooltip />
                <Line type="monotone" dataKey="score" stroke="#238636" strokeWidth={3} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        </div>
        
        <div className="bg-surface p-6 rounded-xl border border-border">
          <h3 className="text-xl font-bold mb-4">Issues by Category</h3>
          <div className="h-64 flex items-center justify-center text-textMuted">
            (Bar Chart Placeholder)
          </div>
        </div>
      </div>
    </div>
  );
}
