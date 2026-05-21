import { useParams } from 'react-router-dom';

export default function PRDetail() {
  const { id } = useParams();
  
  return (
    <div className="max-w-4xl mx-auto py-8 px-4 text-white">
      <h2 className="text-3xl font-bold mb-4">PR Review Detail</h2>
      <p>Details for PR {id} will go here.</p>
    </div>
  );
}
