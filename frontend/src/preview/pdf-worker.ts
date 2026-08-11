/*
 * pdfjs 装配（fe-doc-preview）：worker 经 vite `new URL(..., import.meta.url)` 资产模式引入，
 * Range 分段加载由 pdfjs 自管；文本层样式取 react-pdf 官方 CSS（无标注层——夹具 PDF 无 annotations）。
 */

import { pdfjs } from 'react-pdf';
import 'react-pdf/dist/Page/TextLayer.css';

pdfjs.GlobalWorkerOptions.workerSrc = new URL('pdfjs-dist/build/pdf.worker.min.mjs', import.meta.url).toString();
