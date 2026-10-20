import { useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Upload, Star, Trash2 } from "lucide-react";
import { deleteImage, getProduct, setMainImage, uploadImage } from "@/lib/api";
import type { Product } from "@/types/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function ProductImages({
  product,
  open,
  onClose,
}: {
  product: Product;
  open: boolean;
  onClose: () => void;
}) {
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const { data } = useQuery({
    queryKey: ["product", product.id],
    queryFn: () => getProduct(product.id),
    enabled: open,
  });
  const images = data?.images ?? [];

  const invalidate = () => qc.invalidateQueries({ queryKey: ["product", product.id] });

  const upload = useMutation({
    mutationFn: (file: File) => uploadImage(product.id, file),
    onSuccess: invalidate,
  });
  const setMain = useMutation({
    mutationFn: (imageId: number) => setMainImage(product.id, imageId),
    onSuccess: invalidate,
  });
  const remove = useMutation({ mutationFn: (imageId: number) => deleteImage(product.id, imageId), onSuccess: invalidate });

  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Imágenes</DialogTitle>
          <DialogDescription>{product.name}</DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-4">
          <div>
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) upload.mutate(f);
                e.target.value = "";
              }}
            />
            <Button variant="outline" onClick={() => fileRef.current?.click()} disabled={upload.isPending}>
              <Upload />
              {upload.isPending ? "Subiendo…" : "Subir imagen"}
            </Button>
          </div>

          {images.length === 0 ? (
            <p className="text-sm text-muted-foreground">Sin imágenes todavía.</p>
          ) : (
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              {images.map((img) => (
                <div key={img.id} className="flex flex-col overflow-hidden rounded-lg border">
                  <div className="aspect-square bg-muted">
                    {img.url ? (
                      <img src={img.url} alt="" className="h-full w-full object-cover" />
                    ) : (
                      <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
                        sin URL
                      </div>
                    )}
                  </div>
                  <div className="flex items-center justify-between gap-1 p-2">
                    {img.is_main ? (
                      <Badge variant="success">principal</Badge>
                    ) : (
                      <Button variant="ghost" size="sm" onClick={() => setMain.mutate(img.id)}>
                        <Star />
                        Principal
                      </Button>
                    )}
                    <Button variant="ghost" size="icon" onClick={() => remove.mutate(img.id)}>
                      <Trash2 className="text-destructive" />
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="mt-4 flex justify-end">
          <Button onClick={onClose}>Cerrar</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
