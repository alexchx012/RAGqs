import { Link } from 'react-router';
import { copy } from '../copy';

export function NotFoundPage() {
  return (
    <div className="mx-auto max-w-[var(--page-max-width)] px-5 py-10">
      <h1 className="font-signifier text-heading-sm leading-heading-sm tracking-heading-sm font-normal">
        {copy.shell.notFoundTitle}
      </h1>
      <p className="mt-4 text-body">
        <Link to="/" className="text-slate-gray hover:text-ink-black">
          {copy.shell.notFoundBack}
        </Link>
      </p>
    </div>
  );
}
