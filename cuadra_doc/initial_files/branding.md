Para que este negocio funcione en Cuba, la identidad **no puede parecer un bot corporativo frío ni un canal de spam**. 

Debe transmitir dos cosas fundamentales:
1. **Para el cliente:** Confianza, rapidez y claridad (en Cuba la gente teme que la estafen o le vendan gato por liebre).
2. **Para el TCP:** Profesionalismo y efectividad comercial (que sienta que tiene a un agente de ventas serio trabajando para él).

Aquí tienes la propuesta completa de **Naming, Branding y Concepto Visual**.

---

### 1. Naming (Propuesta Principal y Alternativas)

#### 🏆 Opción Ganadora: **CUADRA** (o **Cuadra.ia**)
* **Por qué funciona:** En el argot cubano, *"cuadrar"* es el verbo universal de los negocios informales y formales (*"ya cuadré con el socio"*, *"eso está cuadrado"*, *"vamos a cuadrar la entrega"*).
* **Sensación:** Suena a fintech moderna (como *Square* o *Block* en EE.UU.), es corto (6 letras), fácil de recordar, contundente y 100% nuestro.
* **Dominio / Redes:** `cuadracuba`, `@cuadra_demo`, `cuadra_ventas`.

#### Otras dos opciones sólidas:
* **ResuelvIA:** Enfatiza la solución inmediata (*"El que te resuelve"*). Muy popular, aunque un poco más informal.
* **Nexo Comercial:** Más sobrio y formal, útil si planeas captar MiPyMEs y negocios más grandes que busquen una imagen corporativa.

---

### 2. Slogan y Mensaje de Marca (Taglines)

* **Slogan Principal:** *"El trato hecho, al momento."*
* **Para Clientes:** *"Lo que buscas, al precio que es y puesto en tus manos."*
* **Para TCPs / Proveedores:** *"Tus productos en movimiento, tus comisiones aseguradas."*

---

### 3. Identidad Visual y Paleta de Colores

El logo debe verse impecable **en un avatar circular de 32x32 píxeles** en WhatsApp y Telegram. Nada de detalles diminutos ni letras pequeñas ilegibles.

#### Paleta de Color:
* **Azul Petróleo Profundo (`#0F172A`):** Transmite estabilidad, seriedad comercial y tecnología sólida. Fondo principal.
* **Verde Eléctrico / Menta (`#10B981` o `#00E599`):** Simboliza dinero, disponibilidad (stock activo) y velocidad de respuesta. Resalta enormemente en el modo oscuro de WhatsApp y Telegram.
* **Blanco Puro (`#FFFFFF`):** Para tipografías limpias y alto contraste.

#### Concepto del Isotipo (El Ícono):
* **La forma:** Una **"C"** geométrica moderna construida con dos flechas o curvas que convergen (representando la unión entre el comprador y el TCP).
* **El detalle:** En el punto de unión de las flechas, un pequeño destello o punto brillante que simula el "cierre del trato" o el nodo de conexión inteligente.

> [!TIP] Prompt para Generadores de Imágenes (Midjourney, DALL-E, Ideogram, Flux):
> *"Minimalist geometric vector logo for a modern automated sales tech brand named 'CUADRA'. Symbol: sleek stylized letter 'C' made of two connecting nodes or overlapping rounded arrows, forming a loop. Dark slate navy background (#0F172A), glowing mint green (#00E599) and crisp white accents. Clean, flat design, suitable for a mobile app icon, high contrast, vector style, no realistic textures, no clutter."*

---

### 4. Tono de Comunicación (Voz de la Marca)

El agente debe tener una personalidad definida en los prompts del LLM:

* **Trato con el Cliente:**
  * **Tono:** Educado, ágil, servicial, sin rodeos burocráticos.
  * **Vocabulario:** Español neutro pero con soltura cubana educada. No usa "Estimado usuario", sino: *"¡Buenas! Claro que sí, tenemos disponibilidad..."*.
  * **Regla de oro:** Siempre dar el precio claro, la moneda y si incluye o no transporte. En Cuba la falta de precio en publicaciones genera rechazo.
* **Trato con el TCP:**
  * **Tono:** Directo, respetuoso, enfocado en números y confirmación rápida.
  * **Ejemplo:** *"Buenas socio, tengo un cliente listo para cerrar el EcoFlow de 250 USD. Confírmame si tienes entrega para hoy a Playa"*.

---

### 5. Configuración de los Perfiles en Canales

#### En WhatsApp (Perfil de Empresa / Evolution API):
* **Nombre:** Cuadra | Catálogo & Ventas
* **Foto de Perfil:** Isotipo de la "C" verde menta sobre fondo azul petróleo (sin texto pequeño).
* **Descripción / Info:** 
  > *"Catálogo de tecnología, equipos y hogar con entrega directa en La Habana. Compras seguras y verificación al recibir.*  
  > 🕒 *Atención automática 24/7 | Pagos en USD/CUP"*.

#### En Telegram:
* **Bot de Atención:** `@cuadra_ventas_demo`
* **Canal/Catálogo de Difusión:** `t.me/cuadracatalogo`
* **Mensaje fijado en el canal:** Formato limpio con links de contacto directo al bot para consultar disponibilidad con un solo clic.

---

### 6. Snippet Markdown para agregar a tu Obsidian

Copia este bloque y pégalo al final de tu archivo `Contexto_Proyecto_Gestor_IA.md` para que la IA que programe los prompts del bot conozca la voz de la marca:

```markdown
---
## 7. Branding e Identidad Operativa

* **Nombre de Marca:** Cuadra (Cuadra.ia)
* **Slogan:** *"El trato hecho, al momento."*
* **Colores:** Primario: Navy `#0F172A` | Acento: Menta Eléctrico `#00E599`
* **Arquetipo de IA:** El facilitador ágil y transparente.
* **Directrices del Prompt de Sistema para el LLM:**
  * Nunca sonar como contestadora automática rígida.
  * Siempre confirmar: Producto, Precio exacto, Garantía (si aplica) y Zona de entrega.
  * Respuestas breves (máximo 3 párrafos cortos) adaptadas al ritmo de lectura en WhatsApp/Telegram móvil.
```