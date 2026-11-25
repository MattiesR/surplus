import type { ComponentProps } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

// Shift headings down one level: the page already has its own <h1>, and a document outline needs exactly one.
const shifted: Components = {
  h1: (p: ComponentProps<"h1">) => <h2 className="md-h1" {...p} />,
  h2: (p: ComponentProps<"h2">) => <h3 className="md-h2" {...p} />,
  h3: (p: ComponentProps<"h3">) => <h4 className="md-h3" {...p} />,
  h4: (p: ComponentProps<"h4">) => <h5 {...p} />,
  h5: (p: ComponentProps<"h5">) => <h6 {...p} />,
};

/** Renders project Markdown. Raw HTML in the files is not rendered (react-markdown escapes it by default). */
export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose-md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={shifted}>
        {children}
      </ReactMarkdown>
    </div>
  );
}
