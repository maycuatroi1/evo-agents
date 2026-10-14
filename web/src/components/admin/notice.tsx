// The inline error of a dialog and the words of a failed write moved to components/feedback/notice.tsx, where every
// feature may import them; the files that import them from here keep doing so until they are next changed.
export { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
