"use client";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api, type FunnelCalibrationDTO } from "@/lib/api";

// The decision funnel, exposed (never hidden): shows whether it's off / calibrating
// / live and how much data it has collected, flagged beta.
export function FunnelBetaCard({ userId }: { userId: string }) {
  const [data, setData] = useState<FunnelCalibrationDTO | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .funnelCalibration(userId)
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [userId]);

  // Quiet until loaded; a transparency element should never error the page.
  if (failed || !data) return null;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <CardTitle className="text-sm">Daily decision funnel</CardTitle>
          <Badge variant="secondary" className="text-[11px] uppercase">
            beta
          </Badge>
          <Badge
            variant={data.status === "live" ? "success" : "secondary"}
            className="text-[11px]"
          >
            {data.status === "off"
              ? "not collecting yet"
              : data.status === "collecting"
                ? "calibrating"
                : "live"}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        <p className="text-xs text-muted-foreground">{data.headline}</p>
        {data.status !== "off" && (
          <div className="flex flex-wrap gap-4 text-xs">
            <span>
              <span className="font-semibold">{data.decisions_collected}</span> graded decisions
            </span>
            <span>
              over <span className="font-semibold">{data.days_span}</span> days
            </span>
            <span>
              <span className="font-semibold">{data.would_surface}</span> would surface
            </span>
            {data.surfaced > 0 && (
              <span>
                <span className="font-semibold">{data.surfaced}</span> surfaced to you
              </span>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
