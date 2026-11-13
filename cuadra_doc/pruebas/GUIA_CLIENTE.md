# Guía del cliente — test de Cuadra

Haces de cliente de una tienda por Telegram. Ya estás en sus grupos: ahí ves los
productos publicados. Abre el chat privado con el bot y escríbele como un cliente
normal.

## 1. Tu identidad

Usa **la fila de tu código** tal cual: son datos inventados y no debes cambiarlos.
El organizador te dirá qué código eres. Tu proveedor (TCP) es el de la última
columna; para la compra usa el producto asignado, y para los demás casos elige
productos de ese mismo proveedor.

| Código | Nombre | Zona | Dirección | Teléfono | Producto asignado | Proveedor |
|---|---|---|---|---|---|---|
| C1 | Juan Pérez | Playa | Calle 23 #456, apto 3 | 55512345 | Microondas 20L | Claudia |
| C2 | Carlos Rodríguez | Marianao | Calle 114 #2210 | 55567890 | Ventilador de Torre | Claudia |
| C3 | Miguel Torres | Centro Habana | Calle Águila #312 | 55523456 | Cafetera Express | Claudia |
| C4 | Ana López | La Lisa | Calle 190 #8812 | 55534567 | Combo Desayuno | Roger |
| C5 | Laura Fernández | Marianao | Calle 51 #4410 | 55545678 | Pizza Personal | Roger |
| C6 | Pedro Sánchez | Playa | Avenida 5ta #7802 | 55556789 | Monitor LED 24 | Rolando |
| C7 | Luis Ramírez | Diez de Octubre | Calle 10 #123 | 55567801 | PC de Escritorio Ryzen 5 | Rolando |
| C8 | Marta Díaz | Cerro | Calle 24 #3305 | 55578901 | Aspiradora Portátil | Rolando |
| C9 | José Martínez | Habana del Este | Calle 1ra #220 | 55589012 | Casco para Moto | Yoan |
| C10 | Elena Ruiz | Guanabacoa | Calle 12 #145 | 55590123 | Moto Eléctrica City | Yoan |
| C11 | Daniel Moreno | Regla | Calle Máximo Gómez #88 | 55501234 | Batería para Moto Eléctrica | Yoan |
| C12 | Sofía Romero | Plaza de la Revolución | Calle 2 #5510 | 55513579 | Teclado Mecánico | Rolando |

## 2. Casos a realizar

Hazlos en este orden. No necesitas terminar una conversación para empezar otra.

| #   | Caso               | Qué hacer                                                                                                                                                                                | Preguntas                                                               | Resultado (lo completas tú) |
| --- | ------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- | --------------------------- |
| 1   | Compra completa    | Pide tu producto asignado y su precio. Di que sí y da **zona, dirección y teléfono en mensajes separados**. Confirma cuando el bot te resuma el pedido y espera el aviso de preparación. | ¿Quedó registrada la venta? ¿Qué dato te volvió a pedir o corrigió mal? |                             |
| 2   | Datos desordenados | Compra **otro producto de tu mismo proveedor**, pero da la dirección sin decir la zona, luego la zona de forma aproximada («cerca de…»), y da un teléfono y corrígelo después.           | ¿Guardó bien cada dato? ¿Cuál falló?                                    |                             |
| 3   | Producto agotado   | Pide el **Power Bank 20000** (aparece publicado pero está agotado).                                                                                                                      | ¿Dijo que no hay, ofreció alternativa o intentó venderlo?               |                             |
| 4   | Producto pausado   | Pide el **Router WiFi 6** (aparece publicado pero está pausado).                                                                                                                         | ¿Lo intentó vender o avisó de que no está disponible?                   |                             |
| 5   | Pregunta técnica   | Pregunta algo que el bot no pueda saber solo: compatibilidad, garantía o si entregan en otra provincia.                                                                                  | ¿Consultó al almacén o inventó la respuesta?                            |                             |
| 6   | Zona sin cobertura | Pide entrega en **Habana Vieja** con cualquier producto.                                                                                                                                 | ¿Avisó de que no hay cobertura o aceptó el pedido?                      |                             |
| 7   | Consultas sueltas  | Escribe las **10 formas distintas** de pedir un producto de la tabla de la sección 4.                                                                                                    | Se anota en la tabla de la sección 4.                                   |                             |

## 3. Registro de la conversación

Apunta **una fila por mensaje tuyo**, mientras hablas. Si un mensaje no aporta
nada («ok», «gracias»), puedes omitirlo. La primera fila es un ejemplo.

| # | Hora | Escribí | Quise decir | Esperaba | Respuesta del bot |
|---|---|---|---|---|---|
| 1 | 14:02 | hola, cuánto sale el microondas | producto = Microondas 20L | precio y oferta de reserva | ¡Buenas! Sí, tenemos… |
| 2 | | | | | |
| 3 | | | | | |
| 4 | | | | | |
| 5 | | | | | |
| 6 | | | | | |
| 7 | | | | | |
| 8 | | | | | |
| 9 | | | | | |
| 10 | | | | | |
| 11 | | | | | |
| 12 | | | | | |

## 4. Consultas sueltas (caso 7)

Diez maneras distintas de pedir un producto, como si buscaras en la tienda. No
hace falta conversación: solo una línea por consulta.

| # | Escribí | Producto que quería | ¿Apareció? (sí / no / otro) |
|---|---|---|---|
| 1 | | | |
| 2 | | | |
| 3 | | | |
| 4 | | | |
| 5 | | | |
| 6 | | | |
| 7 | | | |
| 8 | | | |
| 9 | | | |
| 10 | | | |

## 5. Cierre

| Pregunta | Respuesta |
|---|---|
| ¿En qué momento dudó o se equivocó el bot? | |
| ¿Qué te sorprendió, para bien o para mal? | |
| ¿Algo te resultó incómodo o confuso? | |

## 6. Entrega

Guarda el archivo como `C1_registro.md` (con tu código) y mándaselo al
organizador por el canal que te indique.
