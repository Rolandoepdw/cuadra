import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Save } from "lucide-react";
import { getBroadcastStrategy, getDeliverySettings, setBroadcastStrategy, setDeliverySettings } from "@/lib/api";
import type { BroadcastStrategyIn, TransportPolicy } from "@/types/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";

const EMPTY: BroadcastStrategyIn = {
  max_posts_per_day: 20,
  min_interval_s: 300,
  max_interval_s: 1200,
  repeat_passes: 2,
  repeat_window_days: 1,
  apply_to_groups: false,
};

export function StrategyPanel() {
  const qc = useQueryClient();
  const { data, isLoading, error } = useQuery({
    queryKey: ["broadcast-strategy"],
    queryFn: getBroadcastStrategy,
  });
  const [form, setForm] = useState<BroadcastStrategyIn>(EMPTY);

  useEffect(() => {
    if (data) setForm({ ...data, apply_to_groups: false });
  }, [data]);

  const save = useMutation({
    mutationFn: setBroadcastStrategy,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["broadcast-strategy"] });
      qc.invalidateQueries({ queryKey: ["broadcast-groups"] });
    },
  });

  const delivery = useQuery({
    queryKey: ["delivery-settings"],
    queryFn: getDeliverySettings,
  });
  const [policy, setPolicy] = useState<TransportPolicy>("coordinated");
  useEffect(() => {
    if (delivery.data) setPolicy(delivery.data.transport_policy);
  }, [delivery.data]);
  const saveDelivery = useMutation({
    mutationFn: () => setDeliverySettings({ transport_policy: policy }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["delivery-settings"] }),
  });

  const set = <K extends keyof BroadcastStrategyIn>(
    field: K,
    value: BroadcastStrategyIn[K],
  ) => setForm((current) => ({ ...current, [field]: value }));

  const invalidIntervals = form.max_interval_s < form.min_interval_s;

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Estrategia</h1>
        <p className="text-sm text-muted-foreground">
          Ritmo de publicación de difusiones y política de entrega. Los grupos
          pueden sobrescribir el ritmo.
        </p>
      </div>

      <Card className="max-w-2xl">
        <CardHeader>
          <CardTitle className="text-base">Ritmo de publicación</CardTitle>
        </CardHeader>
        <CardContent className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div className="flex flex-col gap-2">
            <Label>Máximo por día</Label>
            <Input
              type="number"
              min={0}
              max={1000}
              value={form.max_posts_per_day}
              onChange={(e) => set("max_posts_per_day", Number(e.target.value))}
            />
            <p className="text-xs text-muted-foreground">Tope de seguridad por grupo.</p>
          </div>
          <div className="flex flex-col gap-2">
            <Label>Repeticiones por ciclo</Label>
            <Input
              type="number"
              min={1}
              max={100}
              value={form.repeat_passes}
              onChange={(e) => set("repeat_passes", Number(e.target.value))}
            />
            <p className="text-xs text-muted-foreground">
              Veces que se publica cada difusión dentro del ciclo.
            </p>
          </div>
          <div className="flex flex-col gap-2">
            <Label>Duración del ciclo (días)</Label>
            <Input
              type="number"
              min={1}
              max={30}
              value={form.repeat_window_days}
              onChange={(e) => set("repeat_window_days", Number(e.target.value))}
            />
            <p className="text-xs text-muted-foreground">
              Al terminar, se reinicia el conteo de repeticiones.
            </p>
          </div>
          <div className="flex flex-col gap-2">
            <Label>Intervalo mínimo (s)</Label>
            <Input
              type="number"
              min={0}
              value={form.min_interval_s}
              onChange={(e) => set("min_interval_s", Number(e.target.value))}
            />
          </div>
          <div className="flex flex-col gap-2">
            <Label>Intervalo máximo (s)</Label>
            <Input
              type="number"
              min={0}
              value={form.max_interval_s}
              onChange={(e) => set("max_interval_s", Number(e.target.value))}
            />
          </div>
          <label className="flex items-center gap-2 text-sm sm:col-span-2">
            <input
              type="checkbox"
              checked={form.apply_to_groups ?? false}
              onChange={(e) => set("apply_to_groups", e.target.checked)}
            />
            Aplicar también a todos los grupos existentes
          </label>
          <p className="text-xs text-muted-foreground sm:col-span-2">
            Las repeticiones y el ciclo son globales. Los límites del grupo se usan
            al crear grupos nuevos; marca la casilla para sobrescribirlos todos.
          </p>
        </CardContent>
      </Card>

      {error && <p className="text-sm text-destructive">{(error as Error).message}</p>}
      {save.error && <p className="text-sm text-destructive">{(save.error as Error).message}</p>}
      {save.isSuccess && (
        <p className="text-sm text-primary">
          Estrategia guardada
          {save.data?.groups !== undefined ? ` y aplicada a ${save.data.groups} grupo(s).` : "."}
        </p>
      )}

      <div>
        <Button
          onClick={() => save.mutate(form)}
          disabled={isLoading || save.isPending || invalidIntervals}
        >
          <Save />
          {save.isPending ? "Guardando…" : "Guardar estrategia"}
        </Button>
        {invalidIntervals && (
          <p className="mt-2 text-sm text-destructive">
            El intervalo máximo no puede ser menor que el mínimo.
          </p>
        )}
      </div>

      <Card className="max-w-2xl">
        <CardHeader>
          <CardTitle className="text-base">Entrega y transporte</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label>Política de transporte</Label>
            <Select
              value={policy}
              onChange={(e) => setPolicy(e.target.value as TransportPolicy)}
            >
              <option value="coordinated">Se coordina al confirmar (por defecto)</option>
              <option value="included">Incluido en el precio</option>
              <option value="not_included">Se cobra aparte</option>
            </Select>
            <p className="text-xs text-muted-foreground">
              Se usa al dar precios. “Se coordina al confirmar” no promete que el
              transporte esté incluido; evita inventar condiciones.
            </p>
          </div>
          <div>
            <Button
              onClick={() => saveDelivery.mutate()}
              disabled={delivery.isLoading || saveDelivery.isPending}
            >
              <Save />
              {saveDelivery.isPending ? "Guardando…" : "Guardar entrega"}
            </Button>
            {saveDelivery.error && (
              <p className="mt-2 text-sm text-destructive">
                {(saveDelivery.error as Error).message}
              </p>
            )}
            {saveDelivery.isSuccess && (
              <p className="mt-2 text-sm text-primary">
                Política de transporte guardada.
              </p>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
