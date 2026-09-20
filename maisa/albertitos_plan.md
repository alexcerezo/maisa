# Este no es el plan que se entrega

El plan que se entrega vive en **`maisa/motor/docs/albertitos_plan.md`**, y el PDF
(`albertitos_plan.pdf`) se genera a partir de él con:

```
python maisa/motor/tools/md_a_pdf.py maisa/motor/docs/albertitos_plan.md albertitos_plan.pdf
```

Este fichero era una copia del plan del 19 de septiembre de 2026 que se quedó atrás. Llegó a
decir que el OCR «todavía es un esqueleto» y que su throughput estaba «por medir», cuando
`maisa/motor/docs/capacidad.md` §4 ya lo tenía medido, y listaba un juego de ADRs distinto del
que se entregó. Se deja esta nota en lugar del texto para que nadie defienda por error unas
cifras que ya no son las del PDF.

El contenido anterior está en el historial de git:

```
git log --follow -- maisa/albertitos_plan.md
```
