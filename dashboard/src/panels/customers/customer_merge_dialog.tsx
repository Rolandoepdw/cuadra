import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";
import { mergeCustomers, previewCustomerMerge } from "@/lib/api";
import type { CustomerMergePreview } from "@/types/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

function Counts({ move }: { move: CustomerMergePreview["move"] }) {
  const entries = [
    ["Conversaciones", move.conversations],
    ["Mensajes", move.messages],
    ["Ventas", move.sales],
    ["Contactos", move.contacts],
    ["Etiquetas", move.tags],
    ["Notas", move.notes],
    ["Señales", move.signals],
  ] as const;
  return (
    <div className="grid grid-cols-2 gap-2 text-sm sm:grid-cols-4">
      {entries.map(([label, value]) => (
        <div key={label} className="rounded-md border p-2">
          <p className="text-xs text-muted-foreground">{label}</p>
          <p className="font-semibold">{value}</p>
        </div>
      ))}
    </div>
  );
}

export function CustomerMergeDialog({
  open,
  onOpenChange,
  customerId,
  onMerged,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  customerId: number;
  onMerged: (intoId: number) => void;
}) {
  const [intoId, setIntoId] = useState("");
  const [reason, setReason] = useState("");
  const target = Number(intoId);
  const valid = Number.isFinite(target) && target > 0 && target !== customerId;

  const preview = useQuery({
    queryKey: ["customer-merge-preview", customerId, target],
    queryFn: () => previewCustomerMerge(customerId, target),
    enabled: open && valid,
    retry: false,
  });

  const merge = useMutation({
    mutationFn: () =>
      mergeCustomers({
        from_id: customerId,
        into_id: target,
        reason: reason.trim() || undefined,
      }),
    onSuccess: (detail) => {
      onOpenChange(false);
      onMerged(detail.customer.id);
    },
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Fusionar cliente</DialogTitle>
          <DialogDescription>
            Mueve el historial del cliente actual al cliente destino. El id
            actual queda marcado como fusionado y redirige al destino. Esta
            acción no se puede deshacer desde el panel.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="merge-into">Id del cliente destino</Label>
            <Input
              id="merge-into"
              type="number"
              min={1}
              placeholder="Ej. 12"
              value={intoId}
              onChange={(event) => setIntoId(event.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              Búscalo en el listado; nunca se fusiona automáticamente por
              teléfono.
            </p>
          </div>

          {preview.isError && (
            <p className="text-sm text-destructive">
              {(preview.error as Error).message}
            </p>
          )}

          {preview.data && (
            <div className="space-y-3">
              <div className="rounded-md border p-3 text-sm">
                <p>
                  <span className="text-muted-foreground">Desde:</span>{" "}
                  {preview.data.from.display_name ??
                    preview.data.from.name ??
                    preview.data.from.id}{" "}
                  (#{preview.data.from.id})
                </p>
                <p>
                  <span className="text-muted-foreground">Hacia:</span>{" "}
                  {preview.data.into.display_name ??
                    preview.data.into.name ??
                    preview.data.into.id}{" "}
                  (#{preview.data.into.id})
                </p>
              </div>
              <Counts move={preview.data.move} />
              {(preview.data.conflicts.tags.length > 0 ||
                preview.data.conflicts.open_signals.length > 0) && (
                <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
                  <p className="flex items-center gap-2 font-medium">
                    <AlertTriangle className="h-4 w-4" />
                    Conflictos que se resolverán automáticamente
                  </p>
                  {preview.data.conflicts.tags.length > 0 && (
                    <p className="mt-1 text-xs">
                      Etiquetas repetidas:{" "}
                      {preview.data.conflicts.tags.map((tag) => (
                        <Badge key={tag} variant="outline" className="mr-1">
                          {tag}
                        </Badge>
                      ))}
                    </p>
                  )}
                  {preview.data.conflicts.open_signals.length > 0 && (
                    <p className="mt-1 text-xs">
                      Señales abiertas repetidas:{" "}
                      {preview.data.conflicts.open_signals.join(", ")} (se suman
                      los conteos).
                    </p>
                  )}
                </div>
              )}
            </div>
          )}

          <div className="space-y-1.5">
            <Label htmlFor="merge-reason">Motivo (opcional)</Label>
            <Textarea
              id="merge-reason"
              rows={2}
              maxLength={500}
              placeholder="Mismo cliente con dos teléfonos"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
            />
          </div>

          {merge.isError && (
            <p className="text-sm text-destructive">
              {(merge.error as Error).message}
            </p>
          )}
        </div>

        <DialogFooter>
          <Button
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={merge.isPending}
          >
            Cancelar
          </Button>
          <Button
            variant="destructive"
            onClick={() => merge.mutate()}
            disabled={!valid || !preview.data || merge.isPending}
          >
            {merge.isPending ? "Fusionando…" : "Fusionar"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
