import type { ExportJob } from "@/lib/api/types";

/** Export jobs keep polling until they leave queued/running. */
export const isFinished = (job: ExportJob | undefined) =>
  job !== undefined && job.state !== "queued" && job.state !== "running";
