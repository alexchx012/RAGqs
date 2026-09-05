import { Link } from 'react-router';
import { copy } from '../copy';

export function NotFoundPage() {
  return (
    <div className="mx-auto max-w-[var(--page-max-width)] px-5 py-10">
      {/* 品牌标识（审查 P3）：与登录页品牌块同形态，404 页也可识别产品 */}
      <div className="flex h-12 w-12 items-center justify-center rounded-[var(--radius-images)] bg-blush-peach font-signifier text-[24px] text-sienna-brown">
        {copy.appName.charAt(0)}
      </div>
      <h1 className="mt-6 font-signifier text-heading-sm leading-heading-sm tracking-heading-sm font-normal">
        {copy.shell.notFoundTitle}
      </h1>
      <p className="mt-4 text-body">
        <Link to="/" className="text-slate-strong hover:text-ink-black">
          {copy.shell.notFoundBack}
        </Link>
      </p>
    </div>
  );
}
